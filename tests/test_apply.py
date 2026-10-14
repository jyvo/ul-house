import copy
import dataclasses
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import pub_fixture as pf
from ul_house import database
from ul_house.sync import apply, protocol
from ul_house.sync.errors import ApplyError, PackageError, ProtocolError, Refused, RevisionMismatch, SyncError

SRC = Path(pf.ROOT) / "src"
SYNC_DIR = SRC / "ul_house" / "sync"
FIXED_NOW = datetime(2026, 10, 5, 12, 30, 0, tzinfo=timezone.utc)
UserTables = pf.USER_TABLES


@pytest.fixture
def db(tmp_path):
    """factory: db(revision) -> client connection loaded with that revision and user data"""
    opened = []

    def make(revision, name=None, **kw):
        conn = pf.client_db_at(tmp_path / (name or f"c{len(opened)}.sqlite"), revision, **kw)
        opened.append(conn)
        return conn

    yield make
    for conn in opened:
        conn.close()


def package_for(r, n, edit=None):
    doc = pf.make_package(r, n)
    if edit:
        edit(doc)
    return pf.load_package(doc)


def apply_range(conn, r, n, edit=None, **kw):
    package, sha = package_for(r, n, edit)
    return apply.apply_package(conn, package, package_sha256=sha, app_version="test", now=lambda: FIXED_NOW, **kw)


def op_of(doc, op, entity, key):
    (found,) = [o for o in doc["ops"] if o["op"] == op and o["entity"] == entity and o["key"] == key]
    return found


def custom(r, n, ops, icons=None):
    doc = {"from_revision": r, "to_revision": n, "dataset_schema_version": 1, "ops": ops,
           "icons": icons or {"pack": None, "pack_sha256": None, "added": []}}
    return pf.load_package(doc)


def apply_custom(conn, r, n, ops):
    package, sha = custom(r, n, ops)
    return apply.apply_package(conn, package, package_sha256=sha, app_version="t")


def snapshot_of_everything(conn):
    return pf.everything(conn, rowid=True)


def assert_unchanged(conn, before):
    assert not conn.in_transaction
    assert snapshot_of_everything(conn) == before


def test_apply_to_the_same_state_as_the_next_snapshot(db):
    for r, n in ((1, 2), (2, 3), (3, 4), (1, 3), (1, 4), (2, 4)):
        conn = db(r)
        apply_range(conn, r, n)
        assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(n), f"{r} -> {n}"
        assert database.applied_revision(conn) == n
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ops behaviours
def test_insert_adds_the_whole_aggregate(db):
    conn = db(1)
    apply_range(conn, 1, 2)
    assert conn.execute("SELECT name, gear_type, last_changed_revision FROM equipment WHERE uid = ?",
                        (pf.Uid.W2,)).fetchone() == ("Sword+", "sword", 2)
    assert conn.execute("SELECT count(*) FROM weapon WHERE uid = ?", (pf.Uid.W2,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM stat WHERE uid = ?", (pf.Uid.W2,)).fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM evolution_edge WHERE to_uid = ?", (pf.Uid.W2,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM evolution_material WHERE to_uid = ?", (pf.Uid.W2,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM evolution_chain WHERE uid = ?", (pf.Uid.W2,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM icon").fetchone()[0] == 6


def test_update_replaces_the_children_and_keeps_the_row(db):
    conn = db(1)
    rowid = conn.execute("SELECT rowid FROM equipment WHERE uid = ?", (pf.Uid.W1,)).fetchone()[0]
    assert conn.execute("SELECT value FROM stat WHERE uid = ? AND tier = 'initial'", (pf.Uid.W1,)).fetchone()[0] == 100
    apply_range(conn, 1, 2)
    stats = dict(conn.execute("SELECT tier, value FROM stat WHERE uid = ? AND label = 'atk'", (pf.Uid.W1,)))
    assert stats == {"initial": 120, "max": 240}
    assert conn.execute("SELECT count(*) FROM stat WHERE uid = ?", (pf.Uid.W1,)).fetchone()[0] == 2
    assert conn.execute("SELECT last_changed_revision, rowid FROM equipment WHERE uid = ?",
                        (pf.Uid.W1,)).fetchone() == (2, rowid)


def test_a_shrinking_child_set_loses_its_old_rows(db):
    """an UPSERT replaces the aggregate: a child row absent from the payload is deleted"""
    conn = db(1)
    package, sha = package_for(1, 2, lambda d: op_of(d, "UPSERT", "equipment", pf.Uid.W1)["data"].update(
        stat=[r for r in op_of(d, "UPSERT", "equipment", pf.Uid.W1)["data"]["stat"] if r["tier"] == "initial"]))
    apply.apply_package(conn, package, package_sha256=sha, app_version="t")
    assert conn.execute("SELECT count(*) FROM stat WHERE uid = ?", (pf.Uid.W1,)).fetchone()[0] == 1


def test_retire_sets_exactly_three_columns(db):
    conn = db(1)
    conn.row_factory = sqlite3.Row
    before = dict(conn.execute("SELECT * FROM equipment WHERE uid = ?", (pf.Uid.G1,)).fetchone())
    inventory_before = pf.dump(conn, ["inventory", "gear_set_slot"], rowid=True)
    apply_range(conn, 1, 2)
    after = dict(conn.execute("SELECT * FROM equipment WHERE uid = ?", (pf.Uid.G1,)).fetchone())
    conn.row_factory = None
    changed = {c for c in before if before[c] != after[c]}
    assert changed == {"state", "retired_revision", "last_changed_revision"}
    assert (after["state"], after["retired_revision"], after["last_changed_revision"]) == ("retired", 2, 2)
    assert pf.dump(conn, ["inventory", "gear_set_slot"], rowid=True) == inventory_before     # users keep the item
    assert conn.execute("SELECT count(*) FROM defensive_gear WHERE uid = ?", (pf.Uid.G1,)).fetchone()[0] == 1
    assert conn.execute("SELECT reason, since_revision FROM uid_retired WHERE uid = ?", (pf.Uid.G1,)).fetchone() == ("gone", 2)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_alias_repoints_the_uid_and_only_the_uid(db):
    conn = db(2)
    before = pf.dump(conn, ["inventory", "saved_ref"], rowid=True)
    other = pf.dump(conn, ["profile", "gear_set", "gear_set_slot"], rowid=True)
    apply_range(conn, 2, 3)
    after = pf.dump(conn, ["inventory", "saved_ref"], rowid=True)

    def repoint(row):
        # inventory: (rowid, inv_id, profile_id, uid, ...) / saved_ref: (rowid, profile_id, uid, ...)
        return tuple(pf.Uid.G2 if v == pf.Uid.G1 else v for v in row)

    for table in ("inventory", "saved_ref"):
        assert after[table] == sorted(repoint(r) for r in before[table]), table
    assert [r[3] for r in after["inventory"]].count(pf.Uid.G2) == 3         # both duplicates and bob's copy
    assert [r[3] for r in after["inventory"]].count(pf.Uid.G1) == 0
    assert (1, pf.Uid.W1) in {(r[1], r[2]) for r in after["saved_ref"]}     # other uids untouched
    assert pf.dump(conn, ["profile", "gear_set", "gear_set_slot"], rowid=True) == other
    assert conn.execute("SELECT stat_slot1, stat_slot2 FROM inventory WHERE inv_id = 1").fetchone() == ("atk", "def")
    assert conn.execute("SELECT created_at FROM saved_ref WHERE profile_id = 2").fetchone()[0] == before["saved_ref"][-1][3]


def test_the_old_saved_ref_dropped_when_new_uid_already_saved(db):
    """on ALIAS old -> new a profile that saved both keeps only the new one"""
    saved = ((1, pf.Uid.G1), (1, pf.Uid.G2), (1, pf.Uid.W1), (2, pf.Uid.G1))
    conn = db(3, user=False)
    pf.seed_user_data(conn, saved=saved)
    conn.execute("UPDATE saved_ref SET created_at = '2026-01-01T00:00:00Z' WHERE profile_id = 1 AND uid = ?", (pf.Uid.G2,))
    alias = protocol.AliasOp(entity="equipment", key=pf.Uid.G1, to=pf.Uid.G2, rev=3, confirmed_commit=pf.COMMIT)
    with pf.transaction(conn):
        apply.apply_ops(conn, [alias])
    refs = conn.execute("SELECT profile_id, uid, created_at FROM saved_ref ORDER BY 1, 2").fetchall()
    assert [(p, u) for p, u, _ in refs] == [(1, pf.Uid.W1), (1, pf.Uid.G2), (2, pf.Uid.G2)]
    assert dict(((p, u), c) for p, u, c in refs)[(1, pf.Uid.G2)] == "2026-01-01T00:00:00Z"
    assert conn.execute("SELECT count(*) FROM inventory WHERE uid = ?", (pf.Uid.G1,)).fetchone()[0] == 0
    assert conn.execute("SELECT inv_id FROM inventory WHERE uid = ? ORDER BY 1", (pf.Uid.G2,)).fetchall() == [(1,), (2,), (4,)]
    assert conn.execute("SELECT count(*) FROM gear_set_slot").fetchone()[0] == 2


def test_e1_function_on_its_own(db):
    conn = db(3, user=False)
    pf.seed_user_data(conn, saved=((1, pf.Uid.G1), (1, pf.Uid.G2), (2, pf.Uid.G1), (2, pf.Uid.W1)))
    with pf.transaction(conn):
        dropped = apply.drop_saved_refs_shadowed_by_alias(conn, "SELECT ?, ?", (pf.Uid.G1, pf.Uid.G2))
    assert dropped == 1
    assert conn.execute("SELECT profile_id, uid FROM saved_ref ORDER BY 1, 2").fetchall() == [
        (1, pf.Uid.G2), (2, pf.Uid.W1), (2, pf.Uid.G1)]
    with pf.transaction(conn):
        assert apply.drop_saved_refs_shadowed_by_alias(conn, "SELECT ?, ?", (pf.Uid.G1, pf.Uid.G2)) == 0


def test_repoint_uid_handles_the_collision_too(db):
    conn = db(3, user=False)
    pf.seed_user_data(conn, saved=((1, pf.Uid.G1), (1, pf.Uid.G2)))
    with pf.transaction(conn):
        changed = apply.repoint_uid(conn, pf.Uid.G1, pf.Uid.G2)
    # repoint
    assert changed >= 3
    assert conn.execute("SELECT uid FROM saved_ref").fetchall() == [(pf.Uid.G2,)]
    with pf.transaction(conn):
        assert apply.repoint_uid(conn, pf.Uid.G1, pf.Uid.G2) == 0


def test_revive_by_upsert_keeps_the_ledger(db):
    conn = db(3)
    assert conn.execute("SELECT state, retired_revision FROM equipment WHERE uid = ?", (pf.Uid.G3,)).fetchone() == ("retired", 2)
    apply_range(conn, 3, 4)
    assert conn.execute("SELECT state, retired_revision, last_changed_revision FROM equipment WHERE uid = ?",
                        (pf.Uid.G3,)).fetchone() == ("live", None, 4)
    assert conn.execute("SELECT since_revision, reason FROM uid_retired WHERE uid = ?", (pf.Uid.G3,)).fetchone() == (2, "out_of_scope")
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4)


def test_delete_removes_an_orphaned_shared_row(db):
    conn = db(2)
    old = pf.EFFECT_OLD["effect_id"]
    assert conn.execute("SELECT count(*) FROM skill_effect WHERE effect_id = ?", (old,)).fetchone()[0] == 1
    apply_range(conn, 2, 3)
    assert conn.execute("SELECT count(*) FROM skill_effect WHERE effect_id = ?", (old,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM effect_link WHERE effect_id = ?", (old,)).fetchone()[0] == 0


def test_deleting_a_still_referenced_row_fails_at_commit_and_changes_nothing(db):
    for entity, key in (("proc", str(pf.PROC["proc_id"])), ("skill_effect", str(pf.EFFECT_A["effect_id"]))):
        conn = db(1, name=f"del-{entity}.sqlite")
        before = snapshot_of_everything(conn)

        def edit(doc, entity=entity, key=key):
            doc["ops"].append({"op": "DELETE", "entity": entity, "key": key, "rev": 2})

        with pytest.raises(ApplyError):
            apply_range(conn, 1, 2, edit)
        assert_unchanged(conn, before)


def test_equipment_and_the_ledger_are_never_deleted(db):
    conn = db(1)
    before = snapshot_of_everything(conn)
    for entity, key in (("equipment", pf.Uid.W1), ("uid_retired", pf.Uid.G1), ("uid_alias", pf.Uid.G1)):
        op = object.__new__(protocol.DeleteOp)
        for name, value in (("entity", entity), ("key", key), ("rev", 2), ("op", "DELETE")):
            object.__setattr__(op, name, value)
        with pytest.raises(PackageError):
            with pf.transaction(conn):
                apply.apply_ops(conn, [op])
        assert_unchanged(conn, before)
        with pytest.raises(ProtocolError):
            protocol.DeleteOp(entity=entity, key=key, rev=2)


def test_retire_and_alias_of_a_non_retirable_entity_are_refused(db):
    conn = db(1)
    before = snapshot_of_everything(conn)
    op = object.__new__(protocol.RetireOp)
    for name, value in (("entity", "item"), ("key", pf.Uid.ITEM), ("rev", 2), ("reason", "gone"), ("op", "RETIRE")):
        object.__setattr__(op, name, value)
    with pytest.raises(PackageError):
        with pf.transaction(conn):
            apply.apply_ops(conn, [op])
    assert_unchanged(conn, before)


def test_history_row_and_result(db):
    conn = db(1)
    package, sha = package_for(1, 3)
    result = apply.apply_package(conn, package, package_sha256=sha, app_version="9.9", now=lambda: FIXED_NOW)
    assert (result.revision, result.from_revision, result.kind) == (3, 1, "update")
    assert dict(result.counts) == {"UPSERT": sum(o.op == "UPSERT" for o in package.ops), "RETIRE": 2, "ALIAS": 1, "DELETE": 1}
    assert result.icons == package.icons
    assert conn.execute("SELECT revision, from_revision, kind, applied_at, app_version, package_sha256 "
                        "FROM sync_history WHERE revision = 3").fetchone() == (3, 1, "update", "2026-10-05T12:30:00Z", "9.9", sha)
    assert database.applied_revision(conn) == 3
    assert not conn.in_transaction


def test_user_data_is_untouched_when_no_alias_applies(db):
    conn = db(1)
    before = pf.dump(conn, UserTables, rowid=True)
    apply_range(conn, 1, 2)
    assert pf.dump(conn, UserTables, rowid=True) == before


def test_revision_checks(db):
    conn = db(3)
    before = snapshot_of_everything(conn)
    package, sha = package_for(1, 2)
    with pytest.raises(RevisionMismatch):
        apply.apply_package(conn, package, package_sha256=sha, app_version="t")
    assert_unchanged(conn, before)
    package, sha = package_for(3, 4)
    # not past local revision
    for from_rev, to_rev in ((3, 3), (3, 2), (3, 1)):
        stale = dataclasses.replace(package, from_revision=from_rev, to_revision=to_rev)
        with pytest.raises(RevisionMismatch):
            apply.apply_package(conn, stale, package_sha256=sha, app_version="t")
        assert_unchanged(conn, before)
    ahead = dataclasses.replace(package, from_revision=4, to_revision=5)
    with pytest.raises(RevisionMismatch):
        apply.apply_package(conn, ahead, package_sha256=sha, app_version="t")
    assert_unchanged(conn, before)


def test_a_package_needs_a_local_revision(db):
    conn = db(None)
    package, sha = package_for(1, 2)
    with pytest.raises(RevisionMismatch):
        apply.apply_package(conn, package, package_sha256=sha, app_version="t")
    assert database.applied_revision(conn) is None


def test_an_unsupported_dataset_schema_is_refused(db):
    conn = db(1)
    before = snapshot_of_everything(conn)
    package, sha = package_for(1, 2)
    with pytest.raises(Refused) as info:
        apply.apply_package(conn, dataclasses.replace(package, dataset_schema_version=2), package_sha256=sha, app_version="t")
    assert info.value.reason == "schema_unsupported"
    assert_unchanged(conn, before)


def test_apply_twice_gives_the_same_database(db):
    """A: the package once, B: the ops twice in one transaction, C: the package, then the ops again"""
    package, sha = package_for(1, 4)
    a, b, c = db(1), db(1), db(1)
    apply.apply_package(a, package, package_sha256=sha, app_version="t")
    with pf.transaction(b):
        apply.apply_ops(b, package.ops)
        apply.apply_ops(b, package.ops)
    apply.apply_package(c, package, package_sha256=sha, app_version="t")
    with pf.transaction(c):
        apply.apply_ops(c, package.ops)
    tables = pf.GAME_TABLES + pf.USER_TABLES
    assert pf.dump(a, tables) == pf.dump(b, tables) == pf.dump(c, tables)
    assert pf.dump(a, pf.GAME_TABLES) == pf.expected_dump(4)
    # the revision check stops a second apply_package before it starts
    with pytest.raises(RevisionMismatch):
        apply.apply_package(a, package, package_sha256=sha, app_version="t")
    assert a.execute("SELECT count(*) FROM sync_history WHERE revision = 4").fetchone()[0] == 1


def test_ledger_rows_never_change(db):
    conn = db(2)
    before = snapshot_of_everything(conn)
    reason_changed = {"op": "UPSERT", "entity": "uid_retired", "key": pf.Uid.G1, "rev": 3,
                      "data": {"uid_retired": {"uid": pf.Uid.G1, "since_revision": 2, "reason": "out_of_scope"}}}
    with pytest.raises(PackageError):
        apply_custom(conn, 2, 3, [reason_changed])
    assert_unchanged(conn, before)
    same = dict(reason_changed, data={"uid_retired": {"uid": pf.Uid.G1, "since_revision": 2, "reason": "gone"}})
    ops = custom(2, 3, [same])[0].ops
    with pf.transaction(conn):
        apply.apply_ops(conn, ops)
        apply.apply_ops(conn, ops)
    assert_unchanged(conn, before)


def test_retire_needs_its_ledger_row(db):
    conn = db(3)
    before = snapshot_of_everything(conn)
    retire = {"op": "RETIRE", "entity": "equipment", "key": pf.Uid.G2, "rev": 4, "reason": "gone"}
    with pytest.raises(PackageError):
        apply_custom(conn, 3, 4, [retire])
    assert_unchanged(conn, before)


def test_retire_of_a_missing_row_is_refused(db):
    conn = db(3)
    before = snapshot_of_everything(conn)
    ledger = {"op": "UPSERT", "entity": "uid_retired", "key": "8888", "rev": 4,
              "data": {"uid_retired": {"uid": "8888", "since_revision": 4, "reason": "gone"}}}
    retire = {"op": "RETIRE", "entity": "equipment", "key": "8888", "rev": 4, "reason": "gone"}
    with pytest.raises(PackageError):
        apply_custom(conn, 3, 4, [ledger, retire])
    assert_unchanged(conn, before)


def test_alias_must_match_its_ledger_row(db):
    conn = db(3)
    before = snapshot_of_everything(conn)
    alias = {"op": "ALIAS", "entity": "equipment", "key": pf.Uid.G1, "to": pf.Uid.G2, "rev": 3,
             "confirmed_commit": pf.OTHER_COMMIT}                       # the ledger says pf.COMMIT
    with pytest.raises(PackageError):
        apply_custom(conn, 3, 4, [dict(alias, rev=4)])
    assert_unchanged(conn, before)
    elsewhere = dict(alias, to=pf.Uid.W1, confirmed_commit=pf.COMMIT, rev=4)
    with pytest.raises(PackageError):
        apply_custom(conn, 3, 4, [elsewhere])
    assert_unchanged(conn, before)


def test_alias_to_a_uid_that_does_not_exist_rolls_back(db):
    conn = db(2)
    before = snapshot_of_everything(conn)
    ledger = {"op": "UPSERT", "entity": "uid_alias", "key": pf.Uid.G1, "rev": 3,
              "data": {"uid_alias": {"old_uid": pf.Uid.G1, "new_uid": "8888", "since_revision": 3,
                                     "confirmed_commit": pf.COMMIT}}}
    alias = {"op": "ALIAS", "entity": "equipment", "key": pf.Uid.G1, "to": "8888", "rev": 3, "confirmed_commit": pf.COMMIT}
    with pytest.raises(ApplyError):
        apply_custom(conn, 2, 3, [alias, ledger])
    assert_unchanged(conn, before)
    assert conn.execute("SELECT count(*) FROM inventory WHERE uid = ?", (pf.Uid.G1,)).fetchone()[0] == 3


@pytest.mark.parametrize("what", ["rev_outside", "rev_at_from", "duplicate_op", "alias_to_self"])
def test_inv6_violations_are_refused_before_begin(db, what):
    conn = db(2)
    before = snapshot_of_everything(conn)
    base = {"op": "UPSERT", "entity": "item", "key": pf.Uid.ITEM, "rev": 3,
            "data": {"item": {"uid": pf.Uid.ITEM, "name": "Iron Ore"}}}
    ops = {"rev_outside": [dict(base, rev=4)],
           "rev_at_from": [dict(base, rev=2)],
           "duplicate_op": [base, copy.deepcopy(base)],
           "alias_to_self": [{"op": "ALIAS", "entity": "equipment", "key": pf.Uid.G1, "to": pf.Uid.G1, "rev": 3,
                              "confirmed_commit": pf.COMMIT}]}[what]
    statements = []
    conn.set_trace_callback(statements.append)
    package, sha = custom(2, 3, ops)
    with pytest.raises(SyncError) as info:
        apply.apply_package(conn, package, package_sha256=sha, app_version="t")
    conn.set_trace_callback(None)
    assert isinstance(info.value, PackageError) and not isinstance(info.value, ApplyError)
    assert not any(s.lstrip().upper().startswith("BEGIN") for s in statements), "refused before BEGIN"
    assert_unchanged(conn, before)


@pytest.mark.parametrize("what", [
    "missing_column", "extra_column", "root_key_mismatch", "unknown_table", "foreign_table", "foreign_owner_kind",
    "edge_not_incident", "child_not_incident", "material_not_incident",
])
def test_malformed_payloads_are_package_errors_and_roll_back(db, what):
    conn = db(1)
    before = snapshot_of_everything(conn)

    def edit(doc):
        data = op_of(doc, "UPSERT", "equipment", pf.Uid.W1)["data"]
        if what == "missing_column":
            del data["equipment"]["cost"]
        elif what == "extra_column":
            data["equipment"]["bogus"] = 1
        elif what == "root_key_mismatch":
            data["equipment"]["uid"] = "424242"
        elif what == "unknown_table":
            data["no_such_table"] = [{"a": 1}]
        elif what == "foreign_table":
            data["item"] = {"uid": "1", "name": "x"}
        elif what == "foreign_owner_kind":
            data["effect_link"].append({"owner_kind": "proc", "owner_id": "1", "ordinal": 1,
                                        "effect_id": pf.EFFECT_A["effect_id"]})
        elif what == "edge_not_incident":
            data["evolution_edge"].append({"kind": "reforge", "from_uid": "11", "to_uid": "12", "evidence": "both",
                                           "from_name": "a", "to_name": "b"})
        elif what == "child_not_incident":
            data["stat"].append({"uid": pf.Uid.W0, "label": "def", "tier": "initial", "value": 1})
        elif what == "material_not_incident":
            data["evolution_material"].append({"kind": "reforge", "from_uid": "11", "to_uid": "12", "ordinal": 1,
                                               "material_kind": "item", "ref_uid": None, "ref_name": "x", "quantity": 1})

    with pytest.raises(PackageError):
        apply_range(conn, 1, 2, edit)
    assert_unchanged(conn, before)


def test_a_duplicate_child_row_fails_instead_of_merging(db):
    conn = db(1)
    before = snapshot_of_everything(conn)

    def edit(doc):
        stats = op_of(doc, "UPSERT", "equipment", pf.Uid.W1)["data"]["stat"]
        stats.append(dict(stats[0]))

    with pytest.raises(SyncError):
        apply_range(conn, 1, 2, edit)
    assert_unchanged(conn, before)


def test_apply_ops_needs_a_transaction(db):
    conn = db(1)
    with pytest.raises(ApplyError):
        apply.apply_ops(conn, package_for(1, 2)[0].ops)


# interrupted transactions
def test_a_failing_op_after_good_ones_rolls_everything_back(db):
    conn = db(1)
    before = snapshot_of_everything(conn)

    def edit(doc):
        doc["ops"].append({"op": "UPSERT", "entity": "item", "key": "7002", "rev": 2, "data": {"item": {"uid": "7002", "name": None}}})

    with pytest.raises(ApplyError):
        apply_range(conn, 1, 2, edit)
    assert_unchanged(conn, before)
    assert database.applied_revision(conn) == 1


def write_count(tmp_path, r, n):
    """how many INSERT/UPDATE/DELETE statements apply_package runs for r -> n (counted on a scratch copy)"""
    conn = pf.client_db_at(tmp_path / f"count-{r}-{n}.sqlite", r)
    seen = []
    conn.set_trace_callback(lambda s: seen.append(s) if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) else None)
    apply_range(conn, r, n)
    conn.close()
    return len(seen)


CRASH_SCRIPT = """
import json, os, sys
from ul_house import database
from ul_house.sync import apply, protocol

db, package_file, crash_at = sys.argv[1], sys.argv[2], int(sys.argv[3])
conn = database.open_database(db)
package = protocol.UpdatePackage.from_json(json.load(open(package_file)))
count = 0

def trace(sql):
    global count
    if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
        count += 1
        if count == crash_at:
            os._exit(17)            # the process dies mid-transaction: no COMMIT, no ROLLBACK

conn.set_trace_callback(trace)
apply.apply_package(conn, package, package_sha256="0" * 64, app_version="t")
os._exit(0)
"""


@pytest.mark.parametrize("where", ["first_write", "middle", "last_write"])
def test_a_crash_mid_apply_leaves_the_old_revision_intact(tmp_path, where):
    path = tmp_path / "crash.sqlite"
    conn = pf.client_db_at(path, 1)
    before = snapshot_of_everything(conn)
    conn.close()
    total = write_count(tmp_path, 1, 4)
    crash_at = {"first_write": 1, "middle": total // 2, "last_write": total}[where]
    package_file = tmp_path / "pkg.json"
    package_file.write_text(json.dumps(pf.make_package(1, 4)))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SRC), str(Path(pf.__file__).parent)]))
    done = subprocess.run([sys.executable, "-c", CRASH_SCRIPT, str(path), str(package_file), str(crash_at)],
                          env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 17, done.stderr
    reopened = database.open_database(path)
    try:
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert snapshot_of_everything(reopened) == before
        assert database.applied_revision(reopened) == 1
        # retry succeeds
        apply_range(reopened, 1, 4)
        assert pf.dump(reopened, pf.GAME_TABLES) == pf.expected_dump(4)
    finally:
        reopened.close()


# insert / replace guard on equipment
REPLACE_RE = re.compile(r"\b(OR\s+REPLACE|REPLACE\s+INTO)\b", re.I)
EQUIPMENT_WRITE_RE = re.compile(
    r"^\s*(?P<verb>INSERT\b.*?\bINTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?[\"`]?equipment[\"`]?(?![\w])", re.I | re.S)


def test_equipment_is_only_ever_upserted_or_updated_trace(db):
    conn = db(1)
    statements = []
    conn.set_trace_callback(statements.append)
    apply_range(conn, 1, 4)
    conn.set_trace_callback(None)
    assert statements, "the trace saw nothing"
    assert not [s for s in statements if REPLACE_RE.search(s)]
    writes = [(m.group("verb").split()[0].upper(), s) for s in statements if (m := EQUIPMENT_WRITE_RE.match(s))]
    assert writes, "no statement wrote equipment"
    for verb, sql in writes:
        assert verb in ("INSERT", "UPDATE"), sql[:120]
        if verb == "INSERT":
            assert re.search(r"ON\s+CONFLICT", sql, re.I) and re.search(r"DO\s+UPDATE", sql, re.I), sql[:200]
    assert not [s for s in statements if re.match(r"\s*DELETE\s+FROM\s+(main\.)?\"?equipment\"?\b", s, re.I)]


TRIPWIRE = ("CREATE TEMP TRIGGER no_equipment_delete BEFORE DELETE ON main.equipment "
            "BEGIN SELECT RAISE(ABORT, 'equipment row deleted'); END")


def arm(conn):
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.execute(TRIPWIRE)


def test_the_tripwire_is_armed(db):
    """control: with the trigger in place INSERT OR REPLACE on equipment aborts and an UPSERT does not"""
    conn = db(1)
    arm(conn)
    row = conn.execute("SELECT * FROM equipment WHERE uid = ?", (pf.Uid.W1,)).fetchone()
    marks = ", ".join("?" * len(row))
    with pytest.raises(sqlite3.IntegrityError, match="equipment row deleted"):
        conn.execute(f"INSERT OR REPLACE INTO equipment VALUES ({marks})", row)
    conn.execute(f"INSERT INTO equipment VALUES ({marks}) ON CONFLICT (uid) DO UPDATE SET name = excluded.name", row)


@pytest.mark.parametrize("r,n", [(1, 2), (1, 3), (1, 4), (2, 4), (3, 4)])
def test_no_package_deletes_an_equipment_row(db, r, n):
    conn = db(r)
    arm(conn)
    apply_range(conn, r, n)
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(n)


def test_equipment_rowids_and_user_references_survive_an_update(db):
    conn = db(1)
    rowids = dict(conn.execute("SELECT uid, rowid FROM equipment"))
    inventory = pf.dump(conn, ["inventory", "gear_set", "gear_set_slot"], rowid=True)
    apply_range(conn, 1, 4)
    after = dict(conn.execute("SELECT uid, rowid FROM equipment"))
    assert {uid: rowid for uid, rowid in after.items() if uid in rowids} == rowids              # no row rewritten
    assert conn.execute("SELECT inv_id FROM inventory WHERE uid = ?", (pf.Uid.W1,)).fetchone() == (3,)
    slots = pf.dump(conn, ["gear_set", "gear_set_slot"], rowid=True)
    assert slots == {k: v for k, v in inventory.items() if k in ("gear_set", "gear_set_slot")}
    assert conn.execute("SELECT count(*) FROM weapon WHERE uid = ?", (pf.Uid.W1,)).fetchone()[0] == 1


def test_no_sync_source_replaces_rows_by_delete_and_insert():
    for path in sorted(SYNC_DIR.glob("*.py")):
        text = path.read_text()
        assert not REPLACE_RE.search(text), f"{path.name} contains OR REPLACE / REPLACE INTO"


USER_WRITE_RE = re.compile(
    r"\b(INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?[\"`]?(inventory|saved_ref|profile|gear_set_slot|gear_set)\b", re.I)


def test_only_apply_py_writes_user_tables():
    """the alias re-point lives in apply.py, snapshot.py only calls it"""
    for path in sorted(SYNC_DIR.glob("*.py")):
        hits = USER_WRITE_RE.findall(path.read_text())
        if path.name == "apply.py":
            assert {h[1].lower() for h in hits} <= {"inventory", "saved_ref"}, "apply.py writes more than the alias tables"
        else:
            assert not hits, f"{path.name} writes a user table"


# order, shared edges
def test_op_order_does_not_matter(db):
    ops = package_for(1, 4)[0].ops
    for name, ordered in (("reversed", ops[::-1]), ("rotated", ops[5:] + ops[:5]),
                          ("equipment first", sorted(ops, key=lambda o: o.entity != "equipment"))):
        conn = db(1)
        with pf.transaction(conn):
            apply.apply_ops(conn, ordered)
        assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4), name


def test_shared_edges_apply_in_either_order(db):
    package, _ = package_for(1, 2)
    ops = list(package.ops)
    first = next(i for i, o in enumerate(ops) if (o.op, o.entity, o.key) == ("UPSERT", "equipment", pf.Uid.W1))
    second = next(i for i, o in enumerate(ops) if (o.op, o.entity, o.key) == ("UPSERT", "equipment", pf.Uid.W2))
    swapped = list(ops)
    swapped[first], swapped[second] = swapped[second], swapped[first]
    results = []
    for ordered in (ops, swapped):
        conn = db(1)
        with pf.transaction(conn):
            apply.apply_ops(conn, ordered)
        results.append(pf.dump(conn, pf.GAME_TABLES))
    assert results[0] == results[1] == pf.expected_dump(2)
    # both endpoints' payloads carry the shared edge and material rows
    doc = pf.make_package(1, 2)
    for uid in (pf.Uid.W1, pf.Uid.W2):
        data = op_of(doc, "UPSERT", "equipment", uid)["data"]
        assert any({r["from_uid"], r["to_uid"]} == {pf.Uid.W1, pf.Uid.W2} for r in data["evolution_edge"])
        assert any({r["from_uid"], r["to_uid"]} == {pf.Uid.W1, pf.Uid.W2} for r in data["evolution_material"])


def test_an_edge_to_an_unshipped_uid_survives_its_endpoint_being_upserted(db):
    conn = db(1)
    ghost = f"SELECT count(*) FROM evolution_edge WHERE to_uid = '{pf.Uid.GHOST}'"
    assert conn.execute(ghost).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM equipment WHERE uid = ?", (pf.Uid.GHOST,)).fetchone()[0] == 0
    apply_range(conn, 1, 2)
    assert conn.execute(ghost).fetchone()[0] == 1
    assert conn.execute("SELECT from_name, to_name FROM evolution_edge WHERE to_uid = ?", (pf.Uid.GHOST,)).fetchone() == (
        "Sword", "Ghost Blade")
    assert conn.execute("SELECT count(*) FROM evolution_material WHERE to_uid = ?", (pf.Uid.GHOST,)).fetchone()[0] == 1
    # an upsert that drops the edge from its (only shipped) endpoint's payload removes it
    package, sha = package_for(2, 3, lambda d: d["ops"].append(
        {"op": "UPSERT", "entity": "equipment", "key": pf.Uid.W1, "rev": 3, "data": dict(
            aggregate_without_ghost(pf.aggregate_at(3, "equipment", pf.Uid.W1)))}))
    apply.apply_package(conn, package, package_sha256=sha, app_version="t")
    assert conn.execute(ghost).fetchone()[0] == 0


def aggregate_without_ghost(aggregate):
    data = copy.deepcopy(aggregate)
    data["evolution_edge"] = [r for r in data["evolution_edge"] if r["to_uid"] != pf.Uid.GHOST]
    data["evolution_material"] = [r for r in data["evolution_material"] if r["to_uid"] != pf.Uid.GHOST]
    return data


def test_apply_result_counts_for_apply_ops(db):
    conn = db(1)
    with pf.transaction(conn):
        counts = apply.apply_ops(conn, package_for(1, 3)[0].ops)
    assert set(counts) == {"UPSERT", "RETIRE", "ALIAS", "DELETE"}
    assert counts["RETIRE"] == 2 and counts["ALIAS"] == 1 and counts["DELETE"] == 1 and counts["UPSERT"] >= 6
