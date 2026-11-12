"""release builder (snapshot, packages, replay, lineage)"""
import copy
import gzip
import hashlib
import inspect
import json
import re
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import duckdb
import pytest
import release_fixtures as F
from release_fixtures import World

from ul_house import ids
from ul_house.build import release
from ul_house.build.release import ContractError, ReleaseError, ReplayError
from ul_house.build.store import IntegrityError
from ul_house.database import entities as E

SHIPPED = E.GAME_TABLES + E.LEDGER_TABLES
SUBTYPES = ("weapon", "defensive_gear", "monster")
KEY_PATTERNS = {
    "equipment": r"[0-9]{1,20}", "item": r"[0-9]{1,20}", "weapon_ability": r"[0-9]{1,20}",
    "uid_retired": r"[0-9]{1,20}", "uid_alias": r"[0-9]{1,20}",
    "proc": r"(0|[1-9][0-9]{0,18})", "proc_family": r"(0|[1-9][0-9]{0,18})",
    "skill_effect": r"(0|[1-9][0-9]{0,18})", "passive_skill": r"(0|[1-9][0-9]{0,18})",
    "icon": r"[0-9a-f]{64}", "element": r"[a-z_]+",
}

# helpers
def base(w: World) -> None:
    effect = w.skill_effect("self", "Raise ATK")
    w.upsert_shared("item", "9001", {"name": "Herb"})
    w.upsert_shared("item", "9002", {"name": "Moss"})
    w.upsert_equipment("1001", name="Alpha", icon=F.png("alpha"))
    w.upsert_equipment("1002", name="Bravo", kind="defensive_gear", gear_type="armor")
    w.upsert_equipment("1003", name="Charlie", kind="monster", skills=[("Bash", effect)])


def build(tmp_path, *steps, window: int = 6, **kw):
    """world starting at base"""
    w = World(tmp_path, window=window, **kw)
    decisions = [w.cycle(base)]
    for step in steps:
        decisions.append(w.cycle(step))
    return w, decisions


def rename(uid: str, name: str):
    return lambda w: w.update_equipment(uid, name=name)


def ops_of(package: dict) -> set[tuple[str, str, str]]:
    return {(o["op"], o["entity"], o["key"]) for o in package["ops"]}


def op(package: dict, kind: str, entity: str, key: str) -> dict:
    (found,) = [o for o in package["ops"] if (o["op"], o["entity"], o["key"]) == (kind, entity, key)]
    return found


def schema(name: str) -> dict:
    path = F.CONTRACT_DIR / f"{name}.schema.json"
    if not path.exists():
        pytest.skip("T4 contract schemas not present")
    return json.loads(path.read_text())


def assert_valid(name: str, obj) -> None:
    import jsonschema
    errors = sorted(jsonschema.Draft202012Validator(schema(name)).iter_errors(obj),
                    key=lambda e: [str(p) for p in e.path])
    assert not errors, "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])


def op_sort_key(o: dict):
    phase = {"UPSERT": 0, "RETIRE": 1, "ALIAS": 2, "DELETE": 3}[o["op"]]
    index = release.OP_ENTITY_ORDER.index(o["entity"])
    if phase == 3:
        index = -index
    return phase, index, E.key_from_text(E.entity(o["entity"]), o["key"])


def sqlite_with_schema() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    sections = E.schema_sections()
    conn.executescript(sections["game"] + sections["ledger"])
    return conn


def test_constants():
    assert release.PACKAGE_WINDOW == 30
    assert release.SNAPSHOT_ONLY_RATIO == 0.5
    assert set(release.OPERATIONS) == {"INSERT", "UPDATE", "RETIRE", "ALIAS", "DELETE"}
    assert set(release.OP_ENTITY_ORDER) == {e.entity_type for e in E.ENTITIES}
    assert release.OP_ENTITY_ORDER[-2:] == ("uid_retired", "uid_alias")


def test_shipped_tables_are_game_plus_ledger():
    tables = release.shipped_tables()
    assert tables == E.GAME_TABLES + E.LEDGER_TABLES
    assert not set(tables) & (set(E.SYNC_TABLES) | set(E.USER_TABLES))


# snapshot
@pytest.fixture
def snapshot_world(tmp_path):
    w = World(tmp_path / "w")
    base(w)
    w.upsert_equipment("1004", name="Delta", kind="monster")
    w.retire("1004")
    w.commit()
    return w


def make_snapshot(w: World, out: Path):
    return release.build_snapshot(w.work / "mart.duckdb", out, revision=w.revision,
                                  dataset_schema_version=E.DATASET_SCHEMA_VERSION)


def test_snapshot_holds_exactly_the_shipped_tables(snapshot_world, tmp_path):
    info = make_snapshot(snapshot_world, tmp_path / "out")
    conn = sqlite3.connect(info.sqlite_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == set(release.shipped_tables())
    assert not tables & (set(E.SYNC_TABLES) | set(E.USER_TABLES))


def test_snapshot_has_only_tables_and_indexes(snapshot_world, tmp_path):
    info = make_snapshot(snapshot_world, tmp_path / "out")
    conn = sqlite3.connect(info.sqlite_path)
    kinds = {r[0] for r in conn.execute("SELECT type FROM sqlite_master")}
    assert kinds <= {"table", "index"}
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view', 'trigger')").fetchall()


def test_snapshot_row_counts_equal_the_mart(snapshot_world, tmp_path):
    info = make_snapshot(snapshot_world, tmp_path / "out")
    conn = sqlite3.connect(info.sqlite_path)
    mart = duckdb.connect(str(snapshot_world.work / "mart.duckdb"), read_only=True)
    for table in release.shipped_tables():
        expected = mart.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        assert conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == expected == info.counts[table], table
    assert info.counts["equipment"] == 4


def test_snapshot_file_format_p15(snapshot_world, tmp_path):
    info = make_snapshot(snapshot_world, tmp_path / "out")
    conn = sqlite3.connect(info.sqlite_path)
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == E.DATASET_SCHEMA_VERSION
    assert conn.execute("SELECT state, retired_revision FROM equipment WHERE uid = '1004'").fetchone() == ("retired", 1)
    assert conn.execute("SELECT uid, reason FROM uid_retired").fetchall() == [("1004", "gone")]


def test_snapshot_column_names_and_types_equal_the_schema(snapshot_world, tmp_path):
    info = make_snapshot(snapshot_world, tmp_path / "out")
    conn = sqlite3.connect(info.sqlite_path)
    reference = sqlite_with_schema()
    for table in release.shipped_tables():
        got = [(r[1], r[2]) for r in conn.execute(f'PRAGMA table_info("{table}")')]
        want = [(r[1], r[2]) for r in reference.execute(f'PRAGMA table_info("{table}")')]
        assert got == want, table


def test_snapshot_gz_roundtrips_and_is_byte_identical_when_rebuilt(snapshot_world, tmp_path):
    a = make_snapshot(snapshot_world, tmp_path / "a")
    b = make_snapshot(snapshot_world, tmp_path / "b")
    assert a.gz_path.read_bytes() == b.gz_path.read_bytes()
    assert a.sha256 == b.sha256 == hashlib.sha256(a.gz_path.read_bytes()).hexdigest()
    assert a.bytes == a.gz_path.stat().st_size
    assert gzip.decompress(a.gz_path.read_bytes()) == a.sqlite_path.read_bytes()
    assert a.sqlite_sha256 == hashlib.sha256(a.sqlite_path.read_bytes()).hexdigest()
    assert a.table_hashes == b.table_hashes
    assert set(a.table_hashes) == set(release.shipped_tables())


def test_snapshot_refuses_a_foreign_key_violation(tmp_path):
    w = World(tmp_path)
    w.upsert_equipment("1001", element="earth")                         # false element row
    w.commit()
    with pytest.raises(ReleaseError):
        make_snapshot(w, tmp_path / "out")


def test_snapshot_wraps_check_violations_in_a_release_error(tmp_path):
    w = World(tmp_path)
    w.upsert_shared("icon", "a" * 64, {"kind": "bogus", "bytes": 1})    # icon.kind CHECK
    w.commit()
    with pytest.raises(ReleaseError, match="icon"):
        make_snapshot(w, tmp_path / "out")


# mart shape
@pytest.fixture
def mart_copy(snapshot_world, tmp_path):
    path = tmp_path / "mart-copy.duckdb"
    path.write_bytes((snapshot_world.work / "mart.duckdb").read_bytes())
    return path


def mutate(path: Path, *statements: str) -> None:
    conn = duckdb.connect(str(path))
    try:
        for s in statements:
            conn.execute(s)
    finally:
        conn.close()


@pytest.mark.parametrize("statements", [
    pytest.param(['ALTER TABLE equipment DROP COLUMN name'], id="missing-column"),
    pytest.param(['ALTER TABLE equipment ADD COLUMN extra VARCHAR'], id="extra-column"),
    pytest.param(['ALTER TABLE equipment ALTER first_seen SET DATA TYPE TIMESTAMP USING first_seen::TIMESTAMP'],
                 id="timestamp-column"),
    pytest.param(['CREATE TABLE sync_history (revision BIGINT)'], id="extra-table"),
    pytest.param(['DROP TABLE element_relation'], id="missing-table"),
])
def test_mart_shape_violations_are_contract_errors(snapshot_world, mart_copy, tmp_path, statements):
    mutate(mart_copy, *statements)
    with pytest.raises(ContractError):
        release.build_snapshot(mart_copy, tmp_path / "out", revision=1,
                               dataset_schema_version=E.DATASET_SCHEMA_VERSION)


def test_contract_and_replay_errors_are_release_errors():
    assert issubclass(ContractError, ReleaseError) and issubclass(ReplayError, ReleaseError)


# table hash
def test_table_hash_ignores_insertion_order_and_sees_value_changes():
    a, b = sqlite_with_schema(), sqlite_with_schema()
    rows = [("1", "Herb"), ("2", "Moss"), ("3", "Fern")]
    a.executemany("INSERT INTO item VALUES (?, ?)", rows)
    b.executemany("INSERT INTO item VALUES (?, ?)", list(reversed(rows)))
    assert release.table_hash(a, "item") == release.table_hash(b, "item")
    b.execute("UPDATE item SET name = 'Fern!' WHERE uid = '3'")
    assert release.table_hash(a, "item") != release.table_hash(b, "item")


def test_table_hash_distinguishes_tables_columns_and_null_from_empty():
    conn = sqlite_with_schema()
    empty = {t: release.table_hash(conn, t) for t in ("item", "weapon_ability")}
    assert empty["item"] != empty["weapon_ability"]                         # header names the table
    conn.execute("INSERT INTO skill_effect VALUES (1, NULL, 'x')")
    with_null = release.table_hash(conn, "skill_effect")
    conn.execute("UPDATE skill_effect SET target = ''")
    assert release.table_hash(conn, "skill_effect") != with_null


def test_table_hashes_cover_every_requested_table(snapshot_world, tmp_path):
    info = make_snapshot(snapshot_world, tmp_path / "out")
    hashes = release.table_hashes(info.sqlite_path, release.shipped_tables())
    assert hashes == dict(info.table_hashes)
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes.values())


# collapsing
def alias_step(old, new):
    return lambda w: w.alias(old, new, F.OTHER_COMMIT)


def retire_both(w: World) -> None:
    w.retire("1001")
    w.retire("1002")


def add_item(w: World) -> None:
    w.upsert_shared("item", "9003", {"name": "Bark"})


COLLAPSE = [
    pytest.param(
        [lambda w: w.upsert_equipment("1004", name="Delta")],
        {("UPSERT", "equipment", "1004")}, id="insert"),
    pytest.param(
        [rename("1001", "A1"), rename("1001", "A2"), rename("1001", "A3")],
        {("UPSERT", "equipment", "1001")}, id="update-x3-is-one-upsert"),
    pytest.param(
        [lambda w: w.retire("1002", "gone")],
        {("RETIRE", "equipment", "1002"), ("UPSERT", "uid_retired", "1002")}, id="retire"),
    pytest.param(
        [lambda w: w.retire("1002"), lambda w: w.revive("1002")],
        {("UPSERT", "equipment", "1002"), ("UPSERT", "uid_retired", "1002")}, id="retire-then-revive"),
    pytest.param(
        [rename("1002", "B1"), lambda w: w.retire("1002")],
        {("UPSERT", "equipment", "1002"), ("UPSERT", "uid_retired", "1002")}, id="update-then-retire"),
    pytest.param(
        [add_item, lambda w: w.delete_shared("item", "9003")],
        set(), id="insert-then-delete-shared-ships-nothing"),
    pytest.param(
        [lambda w: w.upsert_shared("item", "9001", {"name": "Herb+"}), lambda w: w.delete_shared("item", "9001")],
        {("DELETE", "item", "9001")}, id="update-then-delete"),
    pytest.param(
        [lambda w: w.delete_shared("item", "9002"), lambda w: w.upsert_shared("item", "9002", {"name": "Moss2"})],
        {("UPSERT", "item", "9002")}, id="delete-then-insert"),
    pytest.param(
        [retire_both, alias_step("1001", "1002"), alias_step("1002", "1003")],
        {("RETIRE", "equipment", "1001"), ("RETIRE", "equipment", "1002"),
         ("ALIAS", "equipment", "1001"), ("ALIAS", "equipment", "1002"),
         ("UPSERT", "uid_retired", "1001"), ("UPSERT", "uid_retired", "1002"),
         ("UPSERT", "uid_alias", "1001"), ("UPSERT", "uid_alias", "1002")}, id="alias-chain"),
]


@pytest.mark.parametrize("steps, expected", COLLAPSE)
def test_collapsing_rules(tmp_path, steps, expected):
    w, decisions = build(tmp_path, *steps)
    assert ops_of(w.package(decisions[-1], 1)) == expected


def test_update_x3_carries_the_last_revision(tmp_path):
    w, decisions = build(tmp_path, rename("1001", "A1"), rename("1001", "A2"), rename("1001", "A3"))
    package = w.package(decisions[-1], 1)
    assert op(package, "UPSERT", "equipment", "1001")["rev"] == 4
    assert op(package, "UPSERT", "equipment", "1001")["data"]["equipment"]["name"] == "A3"


def test_retire_op_carries_reason_and_revision(tmp_path):
    w, decisions = build(tmp_path, lambda w: w.retire("1002", "out_of_scope"))
    package = w.package(decisions[-1], 1)
    retire = op(package, "RETIRE", "equipment", "1002")
    assert retire == {"op": "RETIRE", "entity": "equipment", "key": "1002", "rev": 2, "reason": "out_of_scope"}
    ledger = op(package, "UPSERT", "uid_retired", "1002")
    assert ledger["data"] == {"uid_retired": {"uid": "1002", "since_revision": 2, "reason": "out_of_scope"}}


def test_revive_ships_the_live_row_and_the_ledger_row_stays(tmp_path):
    w, decisions = build(tmp_path, lambda w: w.retire("1002"), lambda w: w.revive("1002"))
    package = w.package(decisions[-1], 1)
    row = op(package, "UPSERT", "equipment", "1002")["data"]["equipment"]
    assert row["state"] == "live" and row["retired_revision"] is None
    assert ("UPSERT", "uid_retired", "1002") in ops_of(package)


def test_update_then_retire_ships_the_retired_row(tmp_path):
    w, decisions = build(tmp_path, rename("1002", "B1"), lambda w: w.retire("1002"))
    row = op(w.package(decisions[-1], 1), "UPSERT", "equipment", "1002")["data"]["equipment"]
    assert row["state"] == "retired" and row["retired_revision"] == 3 and row["name"] == "B1"


def test_alias_ops_collapse_chains_to_their_end_and_carry_confirmed_commit(tmp_path):
    w, decisions = build(tmp_path, retire_both, alias_step("1001", "1002"), alias_step("1002", "1003"))
    package = w.package(decisions[-1], 1)
    for old in ("1001", "1002"):
        alias = op(package, "ALIAS", "equipment", old)
        assert alias["to"] == "1003" and alias["confirmed_commit"] == F.OTHER_COMMIT
    assert op(package, "ALIAS", "equipment", "1001")["rev"] == 3
    assert op(package, "ALIAS", "equipment", "1002")["rev"] == 4
    assert op(package, "UPSERT", "uid_alias", "1001")["data"]["uid_alias"] == {
        "old_uid": "1001", "new_uid": "1002", "since_revision": 3, "confirmed_commit": F.OTHER_COMMIT}


def test_alias_cycle_is_a_release_error(tmp_path):
    w, _ = build(tmp_path, retire_both)
    w.alias("1001", "1002")
    w.alias("1002", "1001")
    w.commit()
    with pytest.raises(ReleaseError):
        w.release()


def injected(entity, key, op_, *, retire_first=False):
    def step(w):
        if retire_first:
            w.retire("1002")
        w.inject_change(entity, key, op_)
    return step


@pytest.mark.parametrize("step", [
    pytest.param(injected("equipment", "1001", "DELETE"), id="delete-on-equipment"),
    pytest.param(injected("uid_retired", "1002", "UPDATE", retire_first=False), id="update-on-ledger"),
    pytest.param(injected("uid_alias", "1002", "DELETE"), id="delete-on-ledger"),
    pytest.param(injected("bogus", "1", "INSERT"), id="unknown-entity-type"),
    pytest.param(injected("equipment", "1001", "MERGE"), id="unknown-operation"),
])
def test_malformed_change_logs_are_release_errors(tmp_path, step):
    w, _ = build(tmp_path)
    step(w)
    w.commit()
    with pytest.raises(ReleaseError):
        w.release()


def test_upsert_without_a_mart_row_is_a_release_error(tmp_path):
    w, _ = build(tmp_path)
    w.inject_change("equipment", "5555", "INSERT")                      # not in mart
    w.commit()
    with pytest.raises(ReleaseError):
        w.release()


# package shape
def rich(w: World) -> None:
    effect2 = w.skill_effect("self", "Raise DEF")
    w.upsert_equipment("1004", name="Delta", kind="monster", skills=[("Slash", effect2)],
                       edges=[("reforge", "1001", "1004")])
    w.upsert_equipment("1005", name="Echo", icon=F.png("echo"))
    w.add_edge("enlightening", "1003", "7777")                              # 7777 was never crawled
    w.add_material("reforge", "1001", "1004", 1, "Herb", ref_uid="9001", quantity=2)
    w.retire("1002", "out_of_scope")


@pytest.fixture
def rich_world(tmp_path):
    w, decisions = build(tmp_path, rich)
    return w, decisions[-1], w.package(decisions[-1], 1)


def test_package_top_level_keys_have_no_ledger_block(rich_world):
    _, _, package = rich_world
    assert set(package) == {"from_revision", "to_revision", "dataset_schema_version", "ops", "icons"}
    assert (package["from_revision"], package["to_revision"], package["dataset_schema_version"]) == (
        1, 2, E.DATASET_SCHEMA_VERSION)


def test_every_upsert_lists_its_root_and_every_child_table(rich_world):
    _, _, package = rich_world
    upserts = [o for o in package["ops"] if o["op"] == "UPSERT"]
    assert upserts
    for o in upserts:
        e = E.entity(o["entity"])
        children = {c.table for c in e.children}
        required = {e.root_table} | {t for t in children if t not in SUBTYPES}
        present_subtypes = set(o["data"]) & set(SUBTYPES)
        assert set(o["data"]) == required | present_subtypes, (o["entity"], o["key"])
        assert isinstance(o["data"][e.root_table], dict)
        for table in required - {e.root_table}:
            assert isinstance(o["data"][table], list), table
        for table in present_subtypes:
            assert isinstance(o["data"][table], dict)


def test_equipment_has_exactly_one_matching_subtype_object(rich_world):
    _, _, package = rich_world
    expect = {"1001": "weapon", "1002": "defensive_gear", "1003": "monster", "1004": "monster", "1005": "weapon"}
    for uid, subtype in expect.items():
        found = [o for o in package["ops"] if (o["op"], o["entity"], o["key"]) == ("UPSERT", "equipment", uid)]
        if not found:
            continue
        assert set(found[0]["data"]) & set(SUBTYPES) == {subtype}, uid
    assert ("UPSERT", "equipment", "1004") in ops_of(package)


def test_an_edge_between_two_shipped_uids_travels_in_both_endpoint_aggregates(rich_world):
    _, _, package = rich_world
    for uid in ("1001", "1004"):
        edges = op(package, "UPSERT", "equipment", uid)["data"]["evolution_edge"]
        assert [(e["kind"], e["from_uid"], e["to_uid"]) for e in edges] == [("reforge", "1001", "1004")]
        assert edges[0]["from_name"] == "Alpha" and edges[0]["to_name"] == "Delta"
        materials = op(package, "UPSERT", "equipment", uid)["data"]["evolution_material"]
        assert [(m["ref_name"], m["quantity"]) for m in materials] == [("Herb", 2)]


def test_an_edge_to_an_unshipped_uid_travels_with_its_shipped_endpoint_only(rich_world):
    _, _, package = rich_world
    edges = op(package, "UPSERT", "equipment", "1003")["data"]["evolution_edge"]
    assert [(e["kind"], e["from_uid"], e["to_uid"]) for e in edges] == [("enlightening", "1003", "7777")]
    assert not [o for o in package["ops"] if o["entity"] == "equipment" and o["key"] == "7777"]


def test_monster_skills_and_their_effect_links_travel_with_the_monster(rich_world):
    _, _, package = rich_world
    data = op(package, "UPSERT", "equipment", "1004")["data"]
    skill = data["monster_skill"][0]
    assert skill["skill_id"] == ids.skill_id("1004", 1) and isinstance(skill["skill_id"], int)
    assert [(l["owner_kind"], l["owner_id"]) for l in data["effect_link"]] == [
        ("monster_skill", ids.id_to_text(skill["skill_id"]))]


def test_op_order_is_deterministic(rich_world):
    _, _, package = rich_world
    keys = [op_sort_key(o) for o in package["ops"]]
    assert keys == sorted(keys)
    kinds = [o["op"] for o in package["ops"]]
    assert kinds == sorted(kinds, key=["UPSERT", "RETIRE", "ALIAS", "DELETE"].index)


def test_retire_precedes_alias_for_one_key(tmp_path):
    w, decisions = build(tmp_path, retire_both, alias_step("1001", "1003"))
    package = w.package(decisions[-1], 1)
    order = [(o["op"], o["key"]) for o in package["ops"] if o["entity"] == "equipment"]
    assert order.index(("RETIRE", "1001")) < order.index(("ALIAS", "1001"))


def test_delete_ops_come_last_in_reverse_entity_order(tmp_path):
    def delete_things(w):
        w.delete_shared("item", "9001")
        w.upsert_equipment("1004", name="Delta")
    w, decisions = build(tmp_path, delete_things)
    ops = w.package(decisions[-1], 1)["ops"]
    assert ops[-1]["op"] == "DELETE" and ops[-1]["entity"] == "item"
    assert [o["op"] for o in ops].index("DELETE") > [o["op"] for o in ops].index("UPSERT")


def test_at_most_one_op_per_op_entity_key(rich_world):
    _, _, package = rich_world
    triples = [(o["op"], o["entity"], o["key"]) for o in package["ops"]]
    assert len(triples) == len(set(triples))


def test_keys_are_strings_matching_the_p21_patterns(rich_world):
    _, _, package = rich_world
    for o in package["ops"]:
        assert isinstance(o["key"], str)
        assert re.fullmatch(KEY_PATTERNS[o["entity"]], o["key"]), (o["entity"], o["key"])
        assert o["rev"] >= 2 and o["rev"] <= package["to_revision"]


def test_hashed_ids_round_trip_as_ints_and_keys_as_decimal_text(tmp_path):
    effect = ids.effect_id("self", "Raise DEF")
    w, decisions = build(tmp_path, lambda w: w.skill_effect("self", "Raise DEF"))
    package = w.package(decisions[-1], 1)
    o = op(package, "UPSERT", "skill_effect", ids.id_to_text(effect))
    assert o["data"]["skill_effect"]["effect_id"] == effect and isinstance(effect, int)
    assert o["key"] == str(effect)


def test_icons_block_lists_added_icons_and_has_no_pack_bytes(rich_world):
    w, decision, package = rich_world
    echo = hashlib.sha256(F.png("echo")).hexdigest()
    assert set(package["icons"]) == {"pack", "pack_sha256", "added"}
    assert package["icons"]["added"] == [echo]
    assert package["icons"]["pack"] == "icons/packs/delta-1-2.zip"
    manifest = w.manifest(decision)
    (delta,) = manifest["artifacts"]["icon_packs"]["deltas"]
    assert package["icons"]["pack_sha256"] == delta["sha256"]


def test_icons_block_is_null_when_nothing_was_added(tmp_path):
    w, decisions = build(tmp_path, rename("1001", "A1"))
    assert w.package(decisions[-1], 1)["icons"] == {"pack": None, "pack_sha256": None, "added": []}


def test_a_package_row_set_is_the_mart_row_set(tmp_path):
    """upsert data equals the mart's rows"""
    w, decisions = build(tmp_path, rename("1003", "Charlie+"))
    data = op(w.package(decisions[-1], 1), "UPSERT", "equipment", "1003")["data"]
    mart = duckdb.connect(str(w.work / "mart.duckdb"), read_only=True)
    cols = [c[0] for c in F.COLUMNS["equipment"]]
    row = mart.execute("SELECT * FROM equipment WHERE uid = '1003'").fetchone()
    assert data["equipment"] == dict(zip(cols, row))
    assert [s["label"] for s in data["stat"]] == ["atk"]
    assert data["monster"] == {"uid": "1003", "passive_id": None, "restrictions": None}


# cumulative packages
def test_window_of_three_after_five_releases(tmp_path):
    w, decisions = build(tmp_path, rename("1003", "C1"), rename("1001", "B1"), rename("1001", "B2"),
                         rename("1002", "D1"), window=3)
    manifest = w.manifest(decisions[-1])
    assert manifest["revision"] == 5
    assert [u["from_revision"] for u in manifest["artifacts"]["updates"]] == [2, 3, 4]
    assert manifest["minimum_revision"] == 2
    assert manifest["snapshot_only"] is False and manifest["snapshot_only_reasons"] == []
    p2, p3, p4 = (w.package(decisions[-1], r) for r in (2, 3, 4))
    assert ops_of(p2) == {("UPSERT", "equipment", "1001"), ("UPSERT", "equipment", "1002")}
    assert op(p2, "UPSERT", "equipment", "1001")["rev"] == 4
    assert ops_of(p3) == ops_of(p2)
    assert ops_of(p4) == {("UPSERT", "equipment", "1002")}
    assert ("UPSERT", "equipment", "1003") not in ops_of(p2)                # changed in 2: before r = 2


def test_update_artifact_entries_carry_stored_bytes_and_hashes(tmp_path):
    w, decisions = build(tmp_path, rename("1001", "B1"), rename("1002", "D1"))
    decision = decisions[-1]
    manifest = w.manifest(decision)
    for entry in manifest["artifacts"]["updates"]:
        data = (decision.bundle_dir / "pub" / entry["path"]).read_bytes()
        assert entry["path"] == f"updates/{entry['from_revision']}-3.json.gz"
        assert entry["sha256"] == hashlib.sha256(data).hexdigest() and entry["bytes"] == len(data)


# snapshot-only triggers
def test_first_release_is_snapshot_only(tmp_path):
    w = World(tmp_path)
    decision = w.cycle(base)
    manifest = w.manifest(decision)
    assert manifest["snapshot_only"] is True
    assert manifest["snapshot_only_reasons"] == ["first_release"]
    assert manifest["minimum_revision"] == manifest["revision"] == 1
    assert manifest["artifacts"]["updates"] == []
    assert manifest["artifacts"]["icon_packs"]["deltas"] == []


def test_forced_snapshot(tmp_path):
    w, _ = build(tmp_path)
    decision = w.cycle(rename("1001", "B1"), force_snapshot=True)
    manifest = w.manifest(decision)
    assert manifest["snapshot_only_reasons"] == ["forced"] and manifest["snapshot_only"] is True
    assert manifest["minimum_revision"] == 2 and manifest["artifacts"]["updates"] == []
    assert not list((decision.bundle_dir / "pub").glob("updates/*"))


def test_first_release_forced_has_both_reasons_in_enum_order(tmp_path):
    w = World(tmp_path)
    decision = w.cycle(base, force_snapshot=True)
    assert w.manifest(decision)["snapshot_only_reasons"] == ["forced", "first_release"]


def test_dataset_schema_version_change_is_snapshot_only(tmp_path):
    w, _ = build(tmp_path)
    w.set_schema_version(E.DATASET_SCHEMA_VERSION + 1)
    decision = w.cycle(rename("1001", "B1"))
    manifest = w.manifest(decision)
    assert manifest["snapshot_only_reasons"] == ["dataset_schema_version"]
    assert manifest["dataset_schema_version"] == E.DATASET_SCHEMA_VERSION + 1
    assert manifest["minimum_revision"] == 2 and manifest["artifacts"]["updates"] == []


def test_catalog_version_change_is_snapshot_only(tmp_path):
    w, _ = build(tmp_path)
    w.set_catalog_version("ef" * 32)
    decision = w.cycle(rename("1001", "B1"))
    manifest = w.manifest(decision)
    assert manifest["snapshot_only_reasons"] == ["catalog_version"]
    assert manifest["build"]["catalog_version"] == "ef" * 32
    assert manifest["minimum_revision"] == 2 and manifest["artifacts"]["updates"] == []


def test_package_size_over_the_ratio_is_snapshot_only(tmp_path):
    w, _ = build(tmp_path)
    decision = w.cycle(rename("1001", "B1"), snapshot_only_ratio=0.0)
    manifest = w.manifest(decision)
    assert manifest["snapshot_only_reasons"] == ["package_size"]
    assert manifest["minimum_revision"] == 2 and manifest["artifacts"]["updates"] == []


def test_two_reasons_at_once_are_both_recorded_in_enum_order(tmp_path):
    w, _ = build(tmp_path)
    w.set_catalog_version("ef" * 32)
    decision = w.cycle(rename("1001", "B1"), force_snapshot=True)
    assert w.manifest(decision)["snapshot_only_reasons"] == ["catalog_version", "forced"]
    w.set_schema_version(E.DATASET_SCHEMA_VERSION + 1)
    decision = w.cycle(rename("1001", "B2"), force_snapshot=True)
    assert w.manifest(decision)["snapshot_only_reasons"] == ["dataset_schema_version", "forced"]


def test_a_ratio_that_admits_one_package_but_not_two_keeps_only_that_one(tmp_path):
    def big(w):
        for n in range(6):
            w.upsert_equipment(f"20{n:02d}", name=f"Many {n}", icon=F.png(f"many{n}"))
    w, _ = build(tmp_path, big)
    w.update_equipment("1001", name="B1")
    w.commit()
    loose = w.manifest(w.release(snapshot_only_ratio=100.0))
    sizes = {u["from_revision"]: u["bytes"] for u in loose["artifacts"]["updates"]}
    snapshot = loose["artifacts"]["snapshot"]["bytes"]
    assert sizes[1] > sizes[2]
    ratio = (sizes[1] + sizes[2]) / 2 / snapshot
    decision = w.release(snapshot_only_ratio=ratio)
    manifest = w.manifest(decision)
    assert [u["from_revision"] for u in manifest["artifacts"]["updates"]] == [2]
    assert manifest["minimum_revision"] == 2 and manifest["snapshot_only"] is False
    assert manifest["snapshot_only_reasons"] == []


# boundaries and contiguity enforced
def test_a_catalog_change_cuts_the_window(tmp_path):
    w, _ = build(tmp_path, rename("1001", "B1"))
    w.set_catalog_version("ef" * 32)
    w.cycle(rename("1001", "B2"))                              # snapshot-only
    decision = w.cycle(rename("1001", "B3"))                   # only 3 shares catalog
    manifest = w.manifest(decision)
    assert [u["from_revision"] for u in manifest["artifacts"]["updates"]] == [3]
    assert manifest["minimum_revision"] == 3 and manifest["snapshot_only_reasons"] == []


def test_a_burned_revision_ends_the_contiguous_window(tmp_path):
    w, _ = build(tmp_path, rename("1001", "B1"), rename("1001", "B2"))       # published 1..3
    w.burn(3)
    w.cycle(rename("1001", "B3"))                                            # 4
    w.cycle(rename("1001", "B4"))                                            # 5
    decision = w.cycle(rename("1001", "B5"))                                 # 6
    manifest = w.manifest(decision)
    assert [u["from_revision"] for u in manifest["artifacts"]["updates"]] == [4, 5]
    assert manifest["minimum_revision"] == 4 and manifest["snapshot_only"] is False


def test_when_no_base_revision_qualifies_the_release_is_snapshot_only_with_no_base(tmp_path):
    """never a ReleaseError or operator dispatch"""
    w, _ = build(tmp_path, rename("1001", "B1"))
    w.burn(2)
    decision = w.cycle(rename("1001", "B2"))
    assert decision.action == "publish"
    manifest = w.manifest(decision)
    assert manifest["snapshot_only"] is True
    assert manifest["snapshot_only_reasons"] == ["no_base"]
    assert manifest["minimum_revision"] == manifest["revision"] == 3
    assert manifest["artifacts"]["updates"] == []


def test_the_window_never_reaches_below_revision_one(tmp_path):
    w, decisions = build(tmp_path, rename("1001", "B1"), window=30)
    manifest = w.manifest(decisions[-1])
    assert [u["from_revision"] for u in manifest["artifacts"]["updates"]] == [1]


# replay
@pytest.fixture
def replayable(tmp_path):
    w, decisions = build(tmp_path, rich, rename("1001", "B1"))
    decision = decisions[-1]
    package = w.package(decision, 1)
    return {
        "w": w, "decision": decision, "package": package,
        "snapshot": w.store.pub_dir / "snapshots" / "unison-1.sqlite.gz",
        "expected": w.bundle_json(decision)["table_hashes"],
        "tmp": tmp_path / "replay",
    }


def test_replay_passes_for_every_kept_revision(replayable):
    w, decision = replayable["w"], replayable["decision"]
    assert w.bundle_json(decision)["replay_from"] == [1, 2]
    for r in (1, 2):
        release.replay(w.store.pub_dir / "snapshots" / f"unison-{r}.sqlite.gz", w.package(decision, r),
                       replayable["expected"], tmp_dir=replayable["tmp"] / str(r))


def test_replay_fails_when_an_op_is_missing(replayable):
    package = copy.deepcopy(replayable["package"])
    package["ops"] = [o for o in package["ops"] if (o["op"], o["entity"], o["key"]) != ("UPSERT", "equipment", "1005")]
    with pytest.raises(ReplayError) as exc:
        release.replay(replayable["snapshot"], package, replayable["expected"], tmp_dir=replayable["tmp"])
    assert "equipment" in str(exc.value)


def test_replay_fails_when_a_value_in_data_is_changed(replayable):
    package = copy.deepcopy(replayable["package"])
    target = op(package, "UPSERT", "equipment", "1001")
    target["data"]["equipment"]["name"] = "Tampered"
    with pytest.raises(ReplayError) as exc:
        release.replay(replayable["snapshot"], package, replayable["expected"], tmp_dir=replayable["tmp"])
    assert "equipment" in str(exc.value)


def test_replay_names_every_differing_table(replayable):
    package = copy.deepcopy(replayable["package"])
    package["ops"] = [o for o in package["ops"] if o["entity"] not in ("item", "uid_retired")]
    package["ops"] = [o for o in package["ops"] if o["op"] != "RETIRE"]
    with pytest.raises(ReplayError) as exc:
        release.replay(replayable["snapshot"], package, replayable["expected"], tmp_dir=replayable["tmp"])
    assert "uid_retired" in str(exc.value) and "equipment" in str(exc.value)


def test_applying_the_reference_applier_twice_is_idempotent(replayable, tmp_path):
    db = F.gunzip_to(replayable["snapshot"], tmp_path / "once.sqlite")
    conn = sqlite3.connect(db)
    release.apply_ops_reference(conn, replayable["package"]["ops"])
    once = release.table_hashes(db, release.shipped_tables())
    release.apply_ops_reference(conn, replayable["package"]["ops"])
    conn.close()
    assert release.table_hashes(db, release.shipped_tables()) == once == replayable["expected"]


def test_reference_applier_leaves_foreign_keys_intact(replayable, tmp_path):
    db = F.gunzip_to(replayable["snapshot"], tmp_path / "fk.sqlite")
    conn = sqlite3.connect(db)
    release.apply_ops_reference(conn, replayable["package"]["ops"])
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_retire_sets_exactly_the_three_columns(replayable, tmp_path):
    db = F.gunzip_to(replayable["snapshot"], tmp_path / "retire.sqlite")
    conn = sqlite3.connect(db)
    before = conn.execute("SELECT * FROM equipment WHERE uid = '1002'").fetchone()
    release.apply_ops_reference(conn, [{"op": "RETIRE", "entity": "equipment", "key": "1002", "rev": 9,
                                        "reason": "gone"}])
    cols = [r[1] for r in conn.execute("PRAGMA table_info(equipment)")]
    after = conn.execute("SELECT * FROM equipment WHERE uid = '1002'").fetchone()
    changed = {c for c, a, b in zip(cols, before, after) if a != b}
    assert changed <= {"state", "retired_revision", "last_changed_revision"}
    assert dict(zip(cols, after))["state"] == "retired"
    assert dict(zip(cols, after))["retired_revision"] == 9 == dict(zip(cols, after))["last_changed_revision"]


def test_alias_changes_no_game_row(replayable, tmp_path):
    db = F.gunzip_to(replayable["snapshot"], tmp_path / "alias.sqlite")
    before = release.table_hashes(db, release.shipped_tables())
    conn = sqlite3.connect(db)
    release.apply_ops_reference(conn, [{"op": "ALIAS", "entity": "equipment", "key": "1001", "to": "1003",
                                        "rev": 9, "confirmed_commit": F.OTHER_COMMIT}])
    conn.close()
    assert release.table_hashes(db, release.shipped_tables()) == before


def test_the_reference_applier_never_uses_insert_or_replace():
    source = Path(inspect.getsourcefile(release)).read_text()
    assert not re.search(r"OR\s+REPLACE", source, re.IGNORECASE)
    assert "ON CONFLICT" in source.upper()


# ledger only grows
def test_a_shrinking_ledger_is_a_release_error(tmp_path):
    w, _ = build(tmp_path, lambda w: w.retire("1002"))
    w.drop_ledger("uid_retired", "1002")
    w.upsert_equipment("1004", name="Delta")
    w.commit()
    with pytest.raises(ReleaseError):
        w.release()


def test_a_growing_ledger_passes(tmp_path):
    w, decisions = build(tmp_path, lambda w: w.retire("1002"), lambda w: w.retire("1001"))
    assert decisions[-1].action == "publish"


# icons
def sha_of(token: str) -> str:
    return hashlib.sha256(F.png(token)).hexdigest()


def read_pack(path: Path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        return names, json.loads(z.read("index.json")), {n: z.read(n) for n in names if n != "index.json"}


@pytest.fixture
def icon_world(tmp_path):
    w, decisions = build(
        tmp_path,
        lambda w: (w.upsert_equipment("1005", name="Echo", icon=F.png("beta")),
                   w.upsert_equipment("1006", name="Foxtrot", icon=F.png("gamma")))[0],
        lambda w: w.upsert_equipment("1007", name="Golf", icon=F.png("delta")))
    return w, decisions[-1]


def test_base_pack_holds_every_icon_of_n(icon_world):
    w, decision = icon_world
    ref = w.manifest(decision)["artifacts"]["icon_packs"]["base"]
    assert ref["path"] == "icons/packs/base-3.zip" and ref["icon_count"] == 4
    names, index, members = read_pack(decision.bundle_dir / "pub" / ref["path"])
    assert names[0] == "index.json" and names[1:] == sorted(names[1:])
    expected = {f"equipment/{sha_of(t)}.png": F.png(t) for t in ("alpha", "beta", "gamma", "delta")}
    assert members == expected
    for name, data in members.items():
        assert name == f"equipment/{hashlib.sha256(data).hexdigest()}.png"


def test_base_pack_index_is_valid_and_lists_its_members(icon_world):
    w, decision = icon_world
    names, index, members = read_pack(decision.bundle_dir / "pub" / "icons/packs/base-3.zip")
    assert index["name"] == "base-3.zip" and index["pack_type"] == "base"
    assert index["from_revision"] is None and index["to_revision"] == 3
    assert [i["path"] for i in index["icons"]] == names[1:]
    assert all(i["kind"] == "equipment" and i["bytes"] == len(members[i["path"]]) for i in index["icons"])
    assert_valid("icon_pack", index)


def test_delta_packs_hold_only_icons_new_since_r(icon_world):
    w, decision = icon_world
    deltas = {d["from_revision"]: d for d in w.manifest(decision)["artifacts"]["icon_packs"]["deltas"]}
    assert set(deltas) == {1, 2}
    for r, tokens in ((1, {"beta", "gamma", "delta"}), (2, {"delta"})):
        ref = deltas[r]
        assert ref["path"] == f"icons/packs/delta-{r}-3.zip" and ref["icon_count"] == len(tokens)
        names, index, members = read_pack(decision.bundle_dir / "pub" / ref["path"])
        assert set(members) == {f"equipment/{sha_of(t)}.png" for t in tokens}
        assert index["name"].endswith(".zip") and index["pack_type"] == "delta"
        assert (index["from_revision"], index["to_revision"]) == (r, 3)
        assert_valid("icon_pack", index)
        package = w.package(decision, r)
        assert set(package["icons"]["added"]) == {sha_of(t) for t in tokens}
        assert package["icons"]["pack"] == ref["path"]


def test_a_release_with_no_new_icons_has_no_delta_pack(tmp_path):
    w, decisions = build(tmp_path, rename("1001", "B1"))
    assert w.manifest(decisions[-1])["artifacts"]["icon_packs"]["deltas"] == []


def test_packs_are_deterministic_across_independent_builds(tmp_path):
    def script(root):
        w, decisions = build(root, lambda w: w.upsert_equipment("1005", name="Echo", icon=F.png("beta")))
        return w, decisions[-1]
    (_, da), (_, db) = script(tmp_path / "a"), script(tmp_path / "b")
    for rel in ("icons/packs/base-2.zip", "icons/packs/delta-1-2.zip", "snapshots/unison-2.sqlite.gz",
                "updates/1-2.json.gz", "lineage/2.json.gz"):
        assert (da.bundle_dir / "pub" / rel).read_bytes() == (db.bundle_dir / "pub" / rel).read_bytes(), rel
    for zpath in ("icons/packs/base-2.zip", "icons/packs/delta-1-2.zip"):
        with zipfile.ZipFile(da.bundle_dir / "pub" / zpath) as z:
            assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in z.infolist())
            assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())


def test_a_corrupted_asset_body_is_a_release_error(tmp_path):
    w, _ = build(tmp_path)
    def step(w):
        w.upsert_equipment("1005", name="Echo", icon=F.png("beta"))
        w.corrupt_asset(sha_of("beta"))
    step(w)
    w.commit()
    with pytest.raises(ReleaseError):
        w.release()


def test_icon_bytes_come_from_the_store_when_the_seed_lost_them(tmp_path):
    w, _ = build(tmp_path)                                                     # alpha is now in the store
    w.drop_asset(sha_of("alpha"))
    decision = w.cycle(lambda w: w.upsert_equipment("1005", name="Echo", icon=F.png("beta")))
    assert decision.action == "publish"
    _, _, members = read_pack(decision.bundle_dir / "pub" / "icons/packs/base-2.zip")
    assert members[f"equipment/{sha_of('alpha')}.png"] == F.png("alpha")


def test_an_icon_in_neither_seed_nor_store_is_a_release_error(tmp_path):
    w, _ = build(tmp_path)
    w.upsert_equipment("1005", name="Echo", icon=F.png("beta"))
    w.drop_asset(sha_of("beta"))
    w.commit()
    with pytest.raises(ReleaseError):
        w.release()


def test_the_bundle_carries_only_icons_not_yet_in_the_store(tmp_path):
    w, _ = build(tmp_path)
    decision = w.cycle(lambda w: w.upsert_equipment("1005", name="Echo", icon=F.png("beta")))
    carried = sorted(p.name for p in (decision.bundle_dir / "pub" / "icons" / "equipment").glob("*.png"))
    assert carried == [f"{sha_of('beta')}.png"]
    assert w.store.head(f"pub/icons/equipment/{sha_of('alpha')}.png") is not None


def test_the_first_bundle_carries_every_icon(tmp_path):
    w = World(tmp_path)
    decision = w.cycle(base)
    carried = sorted(p.name for p in (decision.bundle_dir / "pub" / "icons" / "equipment").glob("*.png"))
    assert carried == [f"{sha_of('alpha')}.png"]


MANIFEST_KEYS = {"revision", "dataset_schema_version", "api_schema_version", "minimum_revision", "snapshot_only",
                 "snapshot_only_reasons", "freshness", "build", "entity_counts", "artifacts", "icons", "gate"}
BUILD_KEYS = {"build_commit", "transform_run_id", "crawl_run_id", "seed_sha256", "parser_version",
              "catalog_version", "catalog_commit", "policy_version", "policy_commit", "runtime"}


@pytest.fixture
def manifest_world(tmp_path):
    w, decisions = build(tmp_path, rich, rename("1001", "B1"))
    return w, decisions[-1], w.manifest(decisions[-1])


def test_manifest_keys_are_exactly_the_contracts(manifest_world):
    w, decision, manifest = manifest_world
    assert set(manifest) == MANIFEST_KEYS
    assert set(manifest["build"]) == BUILD_KEYS
    assert set(manifest["freshness"]) == {"source_last_updated", "dataset_generated_at"}
    assert set(manifest["artifacts"]) == {"snapshot", "updates", "icon_packs", "lineage"}
    assert set(manifest["icons"]) == {"count", "withdrawn"}
    assert set(manifest["gate"]) == {"level", "warnings"}
    assert set(manifest["build"]["runtime"]) == {"python", "duckdb", "dbt_core", "dbt_duckdb"}


def test_manifest_values_come_from_the_run(manifest_world):
    w, decision, manifest = manifest_world
    build_ = manifest["build"]
    assert manifest["revision"] == 3 and manifest["minimum_revision"] == 1
    assert manifest["dataset_schema_version"] == E.DATASET_SCHEMA_VERSION
    assert build_["build_commit"] == F.COMMIT and build_["catalog_commit"] == F.COMMIT
    assert build_["catalog_version"] == F.CATALOG_VERSION and build_["policy_version"] == F.POLICY_VERSION
    assert build_["policy_commit"] == F.COMMIT
    assert build_["transform_run_id"] == "transform-3" and build_["crawl_run_id"] == "3"
    assert build_["parser_version"] == 7
    assert build_["seed_sha256"] == hashlib.sha256((w.work / "seed.sqlite").read_bytes()).hexdigest()
    assert manifest["freshness"] == {"source_last_updated": F.ts(3), "dataset_generated_at": F.generated_at(3)}
    assert manifest["build"]["runtime"]["duckdb"] == duckdb.__version__


def test_manifest_api_schema_version_is_the_protocols(manifest_world):
    protocol = pytest.importorskip("ul_house.sync.protocol")
    _, _, manifest = manifest_world
    assert manifest["api_schema_version"] == protocol.API_SCHEMA_VERSION


def test_manifest_entity_counts_cover_all_24_tables(manifest_world):
    w, _, manifest = manifest_world
    assert len(SHIPPED) == 24
    assert set(manifest["entity_counts"]) == set(SHIPPED)
    assert manifest["entity_counts"] == w.table_counts()
    assert manifest["icons"] == {"count": manifest["entity_counts"]["icon"], "withdrawn": []}


def test_manifest_artifacts_point_at_the_bundle_files(manifest_world):
    w, decision, manifest = manifest_world
    a = manifest["artifacts"]
    assert a["snapshot"]["path"] == "snapshots/unison-3.sqlite.gz"
    assert a["lineage"]["path"] == "lineage/3.json.gz"
    assert [u["from_revision"] for u in a["updates"]] == [1, 2]
    for ref in (a["snapshot"], a["lineage"], *a["updates"], a["icon_packs"]["base"], *a["icon_packs"]["deltas"]):
        data = (decision.bundle_dir / "pub" / ref["path"]).read_bytes()
        assert ref["sha256"] == hashlib.sha256(data).hexdigest() and ref["bytes"] == len(data)


def test_manifest_file_is_canonical_json(manifest_world):
    _, decision, manifest = manifest_world
    raw = (decision.bundle_dir / "pub" / "manifests" / "3.json").read_bytes()
    assert raw == json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def test_manifest_validates_against_the_contract_schema(manifest_world):
    assert_valid("manifest", manifest_world[2])


def test_packages_validate_against_the_contract_schema(manifest_world):
    w, decision, manifest = manifest_world
    for entry in manifest["artifacts"]["updates"]:
        assert_valid("update_package", w.package(decision, entry["from_revision"]))


def test_a_snapshot_only_manifest_validates(tmp_path):
    w = World(tmp_path)
    assert_valid("manifest", w.manifest(w.cycle(base)))


def test_gate_warnings_are_mapped_into_the_manifest(tmp_path):
    w, _ = build(tmp_path)
    w.gate("warning", [{"check": "icon_missing", "scope": "new_item", "level": "warning", "count": 1,
                        "pct": 0.1, "message": "one icon missing"},
                       {"check": "info_only", "scope": "all", "level": "info", "count": 3, "pct": 0.0, "message": "n"}])
    decision = w.cycle(rename("1001", "B1"))
    gate = w.manifest(decision)["gate"]
    assert gate["level"] == "warning"
    assert gate["warnings"] == [{"check": "icon_missing", "scope": "new_item", "level": "warning", "count": 1,
                                 "pct": 0.1, "message": "one icon missing"}]


def test_invariants_pass_on_a_real_release(manifest_world):
    w, decision, _ = manifest_world
    manifest, packages, packs = w.artifacts(decision)
    release.check_invariants(manifest, packages, packs)


def test_invariant_4_updates_must_be_contiguous_from_minimum_revision(manifest_world):
    w, decision, _ = manifest_world
    manifest, packages, packs = w.artifacts(decision)
    broken = copy.deepcopy(manifest)
    del broken["artifacts"]["updates"][0]
    with pytest.raises(ReleaseError):
        release.check_invariants(broken, packages, packs)
    broken = copy.deepcopy(manifest)
    broken["minimum_revision"] = 2
    with pytest.raises(ReleaseError):
        release.check_invariants(broken, packages, packs)


def test_invariant_5_every_delta_needs_an_update(manifest_world):
    w, decision, _ = manifest_world
    manifest, packages, packs = w.artifacts(decision)
    broken = copy.deepcopy(manifest)
    broken["artifacts"]["icon_packs"]["deltas"][0]["from_revision"] = 99
    with pytest.raises(ReleaseError):
        release.check_invariants(broken, packages, packs)


def test_invariant_7_package_and_manifest_agree_on_the_delta_pack(manifest_world):
    w, decision, _ = manifest_world
    manifest, packages, packs = w.artifacts(decision)
    broken = copy.deepcopy(packages)
    broken[1]["icons"]["pack_sha256"] = "0" * 64
    with pytest.raises(ReleaseError):
        release.check_invariants(manifest, broken, packs)


def test_invariant_9_icon_count_equals_the_icon_table(manifest_world):
    w, decision, _ = manifest_world
    manifest, packages, packs = w.artifacts(decision)
    broken = copy.deepcopy(manifest)
    broken["icons"]["count"] += 1
    with pytest.raises(ReleaseError):
        release.check_invariants(broken, packages, packs)


def test_validate_rejects_a_document_the_schema_forbids(manifest_world):
    w, decision, manifest = manifest_world
    if not F.CONTRACT_DIR.is_dir():
        pytest.skip("T4 contract schemas not present")
    release.validate("manifest", manifest, F.CONTRACT_DIR)
    extra = {**manifest, "surprise": 1}
    with pytest.raises(ContractError):
        release.validate("manifest", extra, F.CONTRACT_DIR)


def test_validate_without_a_contract_dir_skips_and_with_a_missing_schema_fails(manifest_world, tmp_path):
    _, _, manifest = manifest_world
    assert release.validate("manifest", {"anything": 1}, None) is None
    empty = tmp_path / "empty-contract"
    empty.mkdir()
    with pytest.raises(ContractError):
        release.validate("manifest", manifest, empty)


# lineage
def test_lineage_file(tmp_path):
    w, decisions = build(tmp_path, rich)
    decision = decisions[-1]
    lineage = w.lineage(decision)
    assert {"revision", "generated_at", "build", "transform_run", "snapshot", "changes", "items", "icons"} <= set(lineage)
    assert lineage["revision"] == 2 and lineage["build"]["dirty"] is False
    assert lineage["build"]["build_commit"] == F.COMMIT
    assert lineage["transform_run"]["run_id"] == "transform-2"
    assert lineage["snapshot"]["table_hashes"] == w.bundle_json(decision)["table_hashes"]
    rows = F.history_rows(w.work / "history" / "current.duckdb",
                          "SELECT operation, count(*) FROM dataset_changes WHERE revision = 2 GROUP BY 1")
    expected = {op_: 0 for op_ in release.OPERATIONS} | dict(rows)
    assert lineage["changes"] == expected
    assert sorted(i["uid"] for i in lineage["items"]) == sorted(u for (u,) in w.rows["equipment"])
    item = {i["uid"]: i for i in lineage["items"]}["1001"]
    assert {"source_url", "source_hash", "retrieved_at", "crawl_run_id", "keep_reason", "entry_kind", "row_hash",
            "last_changed_revision"} <= set(item)
    assert {(i["kind"], i["sha256"]) for i in lineage["icons"]} == {
        ("equipment", sha_of("alpha")), ("equipment", sha_of("echo"))}


# decisions
def test_an_empty_change_log_is_a_heartbeat_without_a_bundle(tmp_path):
    w, _ = build(tmp_path)
    w.commit()                                                                 # second revision, nothing changed
    decision = w.release()
    assert decision.action == "heartbeat"
    assert decision.revision == 1 and decision.bundle_dir is None
    assert not (w.work / "release" / "2").exists()


def test_a_blocker_holds_the_candidate_with_a_bundle(tmp_path):
    w, _ = build(tmp_path)
    w.gate("blocker", [{"check": "unexplained_churn", "scope": "catalog", "level": "blocker", "count": 9,
                        "pct": 60.0, "message": "too many changes"}])
    w.update_equipment("1001", name="B1")
    w.commit()
    decision = w.release()
    assert decision.action == "hold" and decision.revision == 2 and decision.gate_level == "blocker"
    assert decision.bundle_dir is not None and (decision.bundle_dir / "bundle.json").is_file()
    assert w.bundle_json(decision)["gate"]["level"] == "blocker"
    assert w.manifest(decision)["gate"]["level"] == "blocker"


def test_a_warning_publishes(tmp_path):
    w, _ = build(tmp_path)
    w.gate("warning")
    decision = w.cycle(rename("1001", "B1"))
    assert decision.action == "publish" and decision.gate_level == "warning"


def test_a_gate_error_builds_no_release(tmp_path):
    w, _ = build(tmp_path)
    w.gate("error")
    w.update_equipment("1001", name="B1")
    w.commit()
    with pytest.raises(ReleaseError):
        w.release()
    assert not (w.work / "release" / "2").exists()


def test_a_blocker_with_an_empty_change_log_fails_closed(tmp_path):
    w, _ = build(tmp_path)
    w.gate("blocker")
    w.commit()
    with pytest.raises(ReleaseError):
        w.release()


def test_the_decision_is_written_and_round_trips(tmp_path):
    w, decisions = build(tmp_path, rename("1001", "B1"))
    decision = decisions[-1]
    written = json.loads((w.work / "release" / "decision.json").read_text())
    assert written["action"] == "publish" and written["revision"] == 2 and written["gate_level"] == "pass"
    assert written["bundle"] == "release/2" and isinstance(written["reason"], str)
    assert release.ReleaseDecision.from_mapping(decision.to_mapping()) == decision


def test_a_first_release_without_release_tables_in_the_history(tmp_path):
    """first history comes straight from dbt"""
    w = World(tmp_path, release_tables=False)
    decision = w.cycle(base)
    assert decision.action == "publish"
    assert w.manifest(decision)["snapshot_only_reasons"] == ["first_release"]


# bundle
def test_bundle_json_describes_every_file(tmp_path):
    w, decisions = build(tmp_path, rich)
    decision = decisions[-1]
    info = w.bundle_json(decision)
    assert info["bundle_version"] == 1 and info["revision"] == 2 and info["run_id"] == "run-1"
    assert info["dataset_schema_version"] == E.DATASET_SCHEMA_VERSION and info["dirty"] is False
    assert info["minimum_revision"] == 1 and info["snapshot_only_reasons"] == []
    assert info["gate"]["level"] == "pass"
    manifest_bytes = (decision.bundle_dir / "pub" / "manifests" / "2.json").read_bytes()
    assert info["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert info["replay_from"] == [1] and set(info["table_hashes"]) == set(SHIPPED)
    listed = {f["path"]: f for f in info["files"]}
    on_disk = {p.relative_to(decision.bundle_dir).as_posix() for p in decision.bundle_dir.rglob("*")
               if p.is_file() and p.name != "bundle.json" and p.relative_to(decision.bundle_dir).parts[0] != "tmp"}
    assert set(listed) == on_disk
    for path, f in listed.items():
        data = (decision.bundle_dir / path).read_bytes()
        assert f["sha256"] == hashlib.sha256(data).hexdigest() and f["bytes"] == len(data)
    assert "build/seed.sqlite" in listed and "build/history.duckdb" in listed
    types = {p: f["content_type"] for p, f in listed.items()}
    assert types["pub/manifests/2.json"] == "application/json"
    assert types["pub/snapshots/unison-2.sqlite.gz"] == "application/gzip"
    assert types["pub/updates/1-2.json.gz"] == "application/gzip"
    assert types["pub/icons/packs/base-2.zip"] == "application/zip"
    assert all(t == "image/png" for p, t in types.items() if p.endswith(".png"))
    assert types["build/seed.sqlite"] == "application/vnd.sqlite3"
    assert types["build/history.duckdb"] == "application/octet-stream"


def test_bundle_load_rehashes_every_file(tmp_path):
    w, decisions = build(tmp_path, rename("1001", "B1"))
    decision = w.cycle(rename("1001", "B2"), publish=False)
    bundle = release.Bundle.load(decision.bundle_dir)
    assert bundle.revision == 3 and bundle.manifest()["revision"] == 3
    assert {f.path for f in bundle.pub_files()} <= {f.path for f in bundle.files}
    assert all(f.path.startswith("pub/") for f in bundle.pub_files())
    target = decision.bundle_dir / "pub" / "updates" / "2-3.json.gz"
    target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(IntegrityError):
        release.Bundle.load(decision.bundle_dir)


def test_bundle_carries_the_build_state_of_the_run(tmp_path):
    w, decisions = build(tmp_path)
    decision = decisions[-1]
    assert (decision.bundle_dir / "build" / "seed.sqlite").read_bytes() == (w.work / "seed.sqlite").read_bytes()
    assert (decision.bundle_dir / "build" / "history.duckdb").is_file()


# next revision
def test_next_revision_is_one_more_than_everything_seen(tmp_path):
    w, _ = build(tmp_path, rename("1001", "B1"))
    history = w.work / "history" / "current.duckdb"
    assert release.next_revision(w.store, history) == 3
    assert release.next_revision(w.store, None) == 3
    w.store.put("pub/manifests/7.json", b"{}")                              # a burned number
    assert release.next_revision(w.store, history) == 8
    conn = duckdb.connect(str(history))
    conn.execute("INSERT INTO release VALUES (9, 't', '1', ?, ?, '{}', '{}', ?, 'sig', ?, 1, false, 'pass', 'r')",
                 ["a" * 64, "b" * 64, "c" * 64, F.ts(9)])
    conn.close()
    assert release.next_revision(w.store, history) == 10
    w.store.put("candidates/12/bundle.json", b"{}")                         # held candidates do not count
    assert release.next_revision(w.store, history) == 10


def test_next_revision_of_an_empty_world_is_one(tmp_path):
    from ul_house.build.store import LocalStore
    assert release.next_revision(LocalStore(tmp_path / "empty"), None) == 1


# build info
def test_build_info_prefers_the_github_environment(tmp_path, monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        return subprocess.CompletedProcess(args[0] if args else [], 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: calls.append(a[0]) or "")
    info = release.BuildInfo.collect(tmp_path, {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "12345"})
    assert info.build_commit == "a" * 40 and info.run_id == "12345"
    assert not [c for c in calls if "rev-parse" in " ".join(map(str, c))]
    assert set(info.runtime) >= {"python", "duckdb", "dbt_core", "dbt_duckdb"}


def test_build_info_run_id_is_valid_without_ci(tmp_path):
    info = release.BuildInfo.collect(Path(__file__).resolve().parents[1], {})
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", info.run_id) and info.run_id.startswith("local-")
    assert re.fullmatch(r"[0-9a-f]{40}", info.build_commit)
    assert isinstance(info.dirty, bool)


# carry forward of parse failed shipped uids
def _aggregate_rows(conn: sqlite3.Connection, uid: str) -> dict[str, list[tuple]]:
    """every row of an equipment aggregate"""
    ent = E.entity("equipment")
    where, params = E.root_where(ent, uid)
    out = {ent.root_table: sorted(conn.execute(f"SELECT * FROM {ent.root_table} WHERE {where}", params))}
    for child in ent.children:
        cwhere, cparams = E.child_where(ent, child, uid)
        out.setdefault(child.table, [])
        out[child.table] += sorted(conn.execute(f"SELECT * FROM {child.table} WHERE {cwhere}", cparams))
    return out


def shipped_with_edges(w: World) -> None:
    """base plus monster 1004 linked"""
    base(w)
    effect = w.skill_effect("self", "Raise DEF")
    w.upsert_equipment("1004", name="Delta", kind="monster", skills=[("Slash", effect)],
                       edges=[("reforge", "1001", "1004")])
    w.add_material("reforge", "1001", "1004", 1, "Herb", ref_uid="9001", quantity=2)


def test_p31_parse_failed_uid_keeps_its_whole_aggregate_from_the_previous_snapshot(tmp_path):
    w = World(tmp_path, window=6)
    w.cycle(shipped_with_edges)
    d2 = w.cycle(lambda w: (w.update_equipment("1002", name="Bravo2"), w.fail_parse("1004")))
    assert d2.action == "publish"
    with duckdb.connect(str(w.work / "mart.duckdb"), read_only=True) as mart:           # mart lacks 1004
        assert mart.execute("SELECT count(*) FROM equipment WHERE uid = '1004'").fetchone()[0] == 0
        assert mart.execute("SELECT count(*) FROM evolution_edge WHERE to_uid = '1004'").fetchone()[0] == 0
    snap1 = sqlite3.connect(F.gunzip_to(w.store.pub_dir / "snapshots" / "unison-1.sqlite.gz", tmp_path / "s1.sqlite"))
    snap2 = w.snapshot_db(d2, tmp_path / "s2.sqlite")
    before, after = _aggregate_rows(snap1, "1004"), _aggregate_rows(snap2, "1004")
    assert after == before
    assert before["equipment"] and before["monster_skill"] and before["effect_link"]
    assert before["evolution_edge"] == [("reforge", "1001", "1004", "Alpha", "Delta")]   # 1004 is to_uid
    assert before["evolution_material"]
    # neighbour endpoint 1001 is identical too
    assert _aggregate_rows(snap1, "1001") == _aggregate_rows(snap2, "1001")
    assert snap2.execute("SELECT name FROM equipment WHERE uid = '1002'").fetchone() == ("Bravo2",)
    # no op for it, either direction
    package = w.package(d2, 1)
    assert not [o for o in package["ops"] if o["key"] == "1004" and o["entity"] == "equipment"]
    assert ops_of(package) >= {("UPSERT", "equipment", "1002")}
    # snapshot(1) + package == snapshot(2)
    assert w.bundle_json(d2)["replay_from"] == [1]
    release.replay(w.store.pub_dir / "snapshots" / "unison-1.sqlite.gz", package,
                   w.bundle_json(d2)["table_hashes"], tmp_dir=tmp_path / "replay")
    # recorded in lineage and decision
    carried = w.lineage(d2)["carried_forward"]
    assert [c["uid"] for c in carried["uids"]] == ["1004"] and carried["from_revision"] == 1
    assert carried["uids"][0]["parser_version"] == 7 and carried["uids"][0]["error"] == "boom"
    assert carried["warnings"] == []
    assert "carried_forward" not in w.manifest(d2)
    assert d2.carried_forward == ("1004",)
    assert json.loads((w.work / "release" / "decision.json").read_text())["carried_forward"] == ["1004"]
    assert w.manifest(d2)["entity_counts"]["equipment"] == 4


def test_p31_the_failure_can_span_several_releases(tmp_path):
    w = World(tmp_path, window=6)
    w.cycle(shipped_with_edges)
    w.cycle(lambda w: (w.update_equipment("1002", name="B2"), w.fail_parse("1004")))
    d3 = w.cycle(lambda w: w.update_equipment("1002", name="B3"))          # still failing: carried from 2
    assert d3.carried_forward == ("1004",)
    with _snap(w, d3, tmp_path / "s3.sqlite") as s3, _snap(w, None, tmp_path / "s1.sqlite", rev=1) as s1:
        assert _aggregate_rows(s3, "1004") == _aggregate_rows(s1, "1004")
    assert w.bundle_json(d3)["replay_from"] == [1, 2]


def _snap(w, decision, dest, rev=None):
    from contextlib import closing
    if decision is not None:
        return closing(w.snapshot_db(decision, dest))
    gz = w.store.pub_dir / "snapshots" / f"unison-{rev}.sqlite.gz"
    return closing(sqlite3.connect(F.gunzip_to(gz, dest)))


def test_p31_a_listed_uid_that_never_shipped_is_ignored_with_a_warning(tmp_path):
    w = World(tmp_path, window=6)
    w.cycle(base)
    d2 = w.cycle(lambda w: (w.update_equipment("1001", name="A2"), w.fail_parse("5555", hide=False)))
    assert d2.action == "publish" and d2.carried_forward == ()
    carried = w.lineage(d2)["carried_forward"]
    assert carried["uids"] == [] and carried["from_revision"] is None
    assert len(carried["warnings"]) == 1 and "5555" in carried["warnings"][0]
    with _snap(w, d2, tmp_path / "s2.sqlite") as s2:
        assert s2.execute("SELECT count(*) FROM equipment WHERE uid = '5555'").fetchone()[0] == 0
        assert s2.execute("SELECT count(*) FROM equipment").fetchone()[0] == 3
    assert "carried_forward" not in json.loads((w.work / "release" / "decision.json").read_text())


def test_p31_without_a_previous_snapshot_the_table_is_ignored(tmp_path):
    w = World(tmp_path, window=6)
    w.fail_parse("1001", hide=False)
    d1 = w.cycle(base)
    assert d1.action == "publish" and d1.carried_forward == ()
    carried = w.lineage(d1)["carried_forward"]
    assert carried["uids"] == [] and len(carried["warnings"]) == 1 and "no previous snapshot" in carried["warnings"][0]
    with _snap(w, d1, tmp_path / "s1.sqlite") as s1:
        assert s1.execute("SELECT count(*) FROM equipment WHERE uid = '1001'").fetchone()[0] == 1


def test_p31_a_listed_uid_the_mart_still_has_is_not_overwritten(tmp_path):
    w = World(tmp_path, window=6)
    w.cycle(base)
    d2 = w.cycle(lambda w: (w.update_equipment("1001", name="A2"), w.fail_parse("1001", hide=False)))
    assert d2.carried_forward == ()
    assert "1001" in w.lineage(d2)["carried_forward"]["warnings"][0]
    with _snap(w, d2, tmp_path / "s2.sqlite") as s2:
        assert s2.execute("SELECT name FROM equipment WHERE uid = '1001'").fetchone() == ("A2",)
    assert ("UPSERT", "equipment", "1001") in ops_of(w.package(d2, 1))


def test_p31_the_main_schema_name_is_accepted_and_a_missing_table_is_empty(tmp_path):
    w = World(tmp_path, window=6)
    w.cycle(shipped_with_edges)
    d2 = w.cycle(lambda w: (w.update_equipment("1002", name="B2"), w.fail_parse("1004", schema="main")))
    assert d2.carried_forward == ("1004",)
    assert release.read_parse_failures(w.work / "mart.duckdb")[0]["uid"] == "1004"
    w.clear_parse_failures(table=False)
    w._write_mart()
    assert release.read_parse_failures(w.work / "mart.duckdb") == []


# shared rows a carried aggregate references
SHARED_TABLES = ("element", "item", "icon", "skill_effect", "proc_family", "proc", "proc_condition",
                 "proc_scaling", "weapon_ability", "passive_skill", "effect_link")
SHARED_ENTITIES = {"proc", "proc_family", "skill_effect", "passive_skill", "weapon_ability", "item", "icon", "element"}


def _table_rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return sorted(conn.execute(f'SELECT * FROM "{table}"'))


def _mart_count(w: World, sql: str) -> int:
    with duckdb.connect(str(w.work / "mart.duckdb"), read_only=True) as mart:
        return mart.execute(sql).fetchone()[0]


def _no_ops_for(package: dict, uid: str) -> None:
    assert not [o for o in package["ops"] if o["entity"] in SHARED_ENTITIES or o["key"] == uid]


def _fk_clean(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def exclusive_weapon(w: World) -> dict:
    """base plus weapon 1010, unshared rows"""
    base(w)
    e_proc = w.skill_effect("enemy", "Slow")
    e_abil = w.skill_effect("self", "Haste")
    proc = w.proc("Blazing Edge", "Edge", effects=[e_proc], conditions=["hp<50", "boss"], scalings=["more at 5XL"])
    ability = w.ability("7001", "Slash Up", effects=[e_abil])
    w.upsert_equipment("1010", name="Echo", icon=F.png("echo"), proc_id=proc, ability_uid=ability)
    w.add_material("reforge", "1010", "1011", 1, "Herb", ref_uid="9001", quantity=3)
    return {"proc": proc, "ability": ability, "effects": (e_proc, e_abil)}


def test_d_int_1_the_shared_rows_only_a_carried_uid_uses_come_along(tmp_path):
    w = World(tmp_path, window=6)
    made: dict = {}
    w.cycle(lambda w: made.update(exclusive_weapon(w)))
    d2 = w.cycle(lambda w: (w.update_equipment("1002", name="Bravo2"), w.fail_parse("1010")))
    assert d2.action == "publish" and d2.carried_forward == ("1010",)
    icon = F.sha256(F.png("echo"))
    proc_text = ids.id_to_text(made["proc"])
    # mart really lacks all of them
    assert _mart_count(w, "SELECT count(*) FROM equipment WHERE uid = '1010'") == 0
    assert _mart_count(w, f"SELECT count(*) FROM proc WHERE proc_id = {made['proc']}") == 0
    assert _mart_count(w, "SELECT count(*) FROM proc_family") == 0
    assert _mart_count(w, "SELECT count(*) FROM proc_condition") == 0
    assert _mart_count(w, "SELECT count(*) FROM proc_scaling") == 0
    assert _mart_count(w, "SELECT count(*) FROM weapon_ability") == 0
    assert _mart_count(w, "SELECT count(*) FROM item WHERE uid = '9001'") == 0
    assert _mart_count(w, f"SELECT count(*) FROM icon WHERE sha256 = '{icon}'") == 0
    assert _mart_count(w, f"SELECT count(*) FROM skill_effect WHERE effect_id IN {made['effects']}") == 0
    assert _mart_count(w, "SELECT count(*) FROM effect_link WHERE owner_kind IN ('proc', 'weapon_ability')") == 0
    assert _mart_count(w, "SELECT count(*) FROM item WHERE uid = '9002'") == 1          # unreferenced: stays
    # snapshot 2 holds them; FKs clean
    with _snap(w, None, tmp_path / "s1.sqlite", rev=1) as s1, _snap(w, d2, tmp_path / "s2.sqlite") as s2:
        _fk_clean(s2)
        for table in SHARED_TABLES:
            assert _table_rows(s2, table) == _table_rows(s1, table), table
        assert s2.execute("SELECT proc_id, ability_uid FROM weapon WHERE uid = '1010'").fetchone() == \
            (made["proc"], "7001")
        assert s2.execute("SELECT count(*) FROM proc_condition WHERE proc_id = ?", (made["proc"],)).fetchone() == (2,)
        assert s2.execute("SELECT count(*) FROM proc_scaling WHERE proc_id = ?", (made["proc"],)).fetchone() == (1,)
        assert s2.execute("SELECT count(*) FROM effect_link WHERE owner_kind = 'proc' AND owner_id = ?",
                          (proc_text,)).fetchone() == (1,)
        assert s2.execute("SELECT icon_sha FROM equipment WHERE uid = '1010'").fetchone() == (icon,)
        assert _aggregate_rows(s2, "1010") == _aggregate_rows(s1, "1010")
    # no op for uid or shared
    package = w.package(d2, 1)
    _no_ops_for(package, "1010")
    assert ops_of(package) >= {("UPSERT", "equipment", "1002")}
    release.replay(w.store.pub_dir / "snapshots" / "unison-1.sqlite.gz", package,
                   w.bundle_json(d2)["table_hashes"], tmp_dir=tmp_path / "replay")
    # lineage records what was copied
    carried = w.lineage(d2)["carried_forward"]
    assert carried["shared_rows"] == {
        "effect_link": 2, "icon": 1, "item": 1, "proc": 1, "proc_condition": 2, "proc_family": 1,
        "proc_scaling": 1, "skill_effect": 2, "weapon_ability": 1}
    assert [c["uid"] for c in carried["uids"]] == ["1010"]
    assert "carried_forward" not in w.manifest(d2)
    # carried icon still in base pack
    assert w.manifest(d2)["entity_counts"]["icon"] == 2
    base_ref = w.manifest(d2)["artifacts"]["icon_packs"]["base"]
    assert icon in json.dumps(w.artifacts(d2)[2][base_ref["path"]])


def test_d_int_1_a_shared_row_the_mart_still_has_is_not_overwritten(tmp_path):
    w = World(tmp_path, window=6)
    made: dict = {}

    def step1(w):
        base(w)
        e = w.skill_effect("enemy", "Slow")
        made["effect"] = e
        made["proc"] = w.proc("Shared Edge", "Edge", effects=[e], conditions=["old a", "old b"], scalings=["old"])
        made["ability"] = w.ability("7001", "Only 1010", effects=[e])
        w.upsert_equipment("1010", name="Echo", icon=F.png("echo"), proc_id=made["proc"], ability_uid="7001")
        w.upsert_equipment("1011", name="Foxtrot", proc_id=made["proc"])          # still parses, same proc

    def step2(w):
        w.fail_parse("1010")
        row = dict(w.rows["proc"][(made["proc"],)])
        row["activation_rate"] = "99%"
        w.upsert_shared("proc", made["proc"], row,
                        proc_condition=[{"proc_id": made["proc"], "ordinal": 1, "condition": "new only"}])

    w.cycle(step1)
    d2 = w.cycle(step2)
    assert d2.carried_forward == ("1010",)
    with _snap(w, None, tmp_path / "s1.sqlite", rev=1) as s1, _snap(w, d2, tmp_path / "s2.sqlite") as s2:
        _fk_clean(s2)
        # the mart's proc wins: new rate
        assert s2.execute("SELECT activation_rate FROM proc WHERE proc_id = ?", (made["proc"],)).fetchone() == ("99%",)
        assert s1.execute("SELECT activation_rate FROM proc WHERE proc_id = ?", (made["proc"],)).fetchone() != ("99%",)
        assert s2.execute("SELECT ordinal, condition FROM proc_condition").fetchall() == [(1, "new only")]
        assert len(s1.execute("SELECT * FROM proc_condition").fetchall()) == 2
        assert s2.execute("SELECT count(*) FROM proc_scaling").fetchone() == (1,)
        # what only 1010 used is carried
        assert _table_rows(s2, "weapon_ability") == _table_rows(s1, "weapon_ability") != []
        assert s2.execute("SELECT count(*) FROM icon WHERE sha256 = ?", (F.sha256(F.png("echo")),)).fetchone() == (1,)
        assert _aggregate_rows(s2, "1010") == _aggregate_rows(s1, "1010")
    carried = w.lineage(d2)["carried_forward"]["shared_rows"]
    assert "proc" not in carried and "proc_condition" not in carried and "proc_family" not in carried
    assert carried["weapon_ability"] == 1 and carried["icon"] == 1
    package = w.package(d2, 1)
    assert ("UPSERT", "proc", ids.id_to_text(made["proc"])) in ops_of(package)
    assert not [o for o in package["ops"] if o["key"] in ("1010", "7001")]
    release.replay(w.store.pub_dir / "snapshots" / "unison-1.sqlite.gz", package,
                   w.bundle_json(d2)["table_hashes"], tmp_dir=tmp_path / "replay")


def test_d_int_1_a_carried_monster_brings_its_passive_and_potential_effects(tmp_path):
    w = World(tmp_path, window=6)
    made: dict = {}

    def step1(w):
        base(w)
        e_skill, e_pass, e_p1, e_p2 = (w.skill_effect("self", d) for d in ("Skill fx", "Passive fx", "Pot 1", "Pot 2"))
        made["effects"] = (e_skill, e_pass, e_p1, e_p2)
        made["passive"] = w.passive("Guard Up", effects=[e_pass])
        w.upsert_equipment("1020", name="Golem", kind="monster", icon=F.png("golem"), skills=[("Smash", e_skill)],
                           passive_id=made["passive"], potentials=[(1, e_p1), (2, e_p2)])

    w.cycle(step1)
    d2 = w.cycle(lambda w: (w.update_equipment("1002", name="Bravo2"), w.fail_parse("1020")))
    assert d2.carried_forward == ("1020",)
    assert _mart_count(w, "SELECT count(*) FROM passive_skill") == 0
    assert _mart_count(w, "SELECT count(*) FROM potential_level") == 0
    # 1003 uses "Raise ATK"; golem gone
    assert _mart_count(w, "SELECT count(*) FROM skill_effect") == 1
    with _snap(w, None, tmp_path / "s1.sqlite", rev=1) as s1, _snap(w, d2, tmp_path / "s2.sqlite") as s2:
        _fk_clean(s2)
        for table in SHARED_TABLES + ("potential_level", "monster_skill", "monster"):
            assert _table_rows(s2, table) == _table_rows(s1, table), table
        assert s2.execute("SELECT passive_id FROM monster WHERE uid = '1020'").fetchone() == (made["passive"],)
        assert s2.execute("SELECT count(*) FROM potential_level WHERE uid = '1020'").fetchone() == (2,)
        owners = {r[0]: r[1] for r in s2.execute(
            "SELECT owner_kind, count(*) FROM effect_link GROUP BY owner_kind")}
        assert owners == {"monster_skill": 2, "passive_skill": 1, "potential_level": 2}      # 1003's skill + golem's
        assert _aggregate_rows(s2, "1020") == _aggregate_rows(s1, "1020")
    carried = w.lineage(d2)["carried_forward"]["shared_rows"]
    assert carried == {"effect_link": 1, "icon": 1, "passive_skill": 1, "skill_effect": 4}
    package = w.package(d2, 1)
    _no_ops_for(package, "1020")
    release.replay(w.store.pub_dir / "snapshots" / "unison-1.sqlite.gz", package,
                   w.bundle_json(d2)["table_hashes"], tmp_dir=tmp_path / "replay")


def test_d_int_1_the_reference_graph_is_the_schema_foreign_keys():
    conn = sqlite3.connect(":memory:")
    conn.executescript(E.schema_sections()["game"])
    graph = release.reference_graph(conn, E.GAME_TABLES)
    roots = release.shared_roots()
    assert roots == {"element", "item", "icon", "skill_effect", "proc_family", "proc", "weapon_ability",
                     "passive_skill"}
    pragma = {(t, fk[3], fk[2], fk[4]) for t in E.GAME_TABLES for fk in conn.execute(f'PRAGMA foreign_key_list("{t}")')}
    logical = {(t, c, p, pc) for (t, c), (p, pc) in release.LOGICAL_REFS.items()}
    flat = {(t, c, p, pc) for t, refs in graph.items() for c, p, pc in refs}
    assert flat == {r for r in pragma if r[2] in roots} | logical
    # left-out FKs point only into aggregate
    assert {r[2] for r in pragma - flat} <= {"equipment", "monster"}
    # FKs from aggregate or shared rows
    assert flat == {
        ("element_relation", "element_id", "element", "element_id"),
        ("element_relation", "related_id", "element", "element_id"),
        ("proc", "family_id", "proc_family", "family_id"),
        ("proc", "element_id", "element", "element_id"),
        ("proc_condition", "proc_id", "proc", "proc_id"),
        ("proc_scaling", "proc_id", "proc", "proc_id"),
        ("proc_scaling", "element_id", "element", "element_id"),
        ("equipment", "element_id", "element", "element_id"),
        ("equipment", "icon_sha", "icon", "sha256"),
        ("weapon", "proc_id", "proc", "proc_id"),
        ("weapon", "ability_uid", "weapon_ability", "uid"),
        ("defensive_gear", "proc_id", "proc", "proc_id"),
        ("monster", "passive_id", "passive_skill", "passive_id"),
        ("potential_level", "effect_id", "skill_effect", "effect_id"),
        ("effect_link", "effect_id", "skill_effect", "effect_id"),
        ("evolution_material", "ref_uid", "item", "uid"),
    }
    for table, refs in graph.items():                       # every named column and parent exists
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        for column, parent, pcolumn in refs:
            assert column in columns
            assert pcolumn in {r[1] for r in conn.execute(f'PRAGMA table_info("{parent}")')}

# duck sql
def test_duck_sql_rewrites_named_parameters():
    assert release.duck_sql("owner_id = :key_text AND uid = :key") == "owner_id = $key_text AND uid = $key"
    assert release.duck_sql("a = :key OR b = :key") == "a = $key OR b = $key"


def test_duck_sql_leaves_casts_alone():
    assert release.duck_sql("CAST(x AS TEXT) = :key_text AND y::TEXT = :key") == \
        "CAST(x AS TEXT) = $key_text AND y::TEXT = $key"
    assert release.duck_sql("owner_id = :key_text::TEXT") == "owner_id = $key_text::TEXT"


def test_every_entity_predicate_converts_cleanly():
    for e in E.ENTITIES:
        for child in e.children:
            converted = release.duck_sql(child.predicate)
            assert not re.search(r"(?<!:):key", converted), child.predicate
            assert "$key" in converted
