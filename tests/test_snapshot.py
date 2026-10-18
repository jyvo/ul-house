import hashlib
from datetime import datetime, timezone

import pytest

import pub_fixture as pf
from local_server import LocalServer, Reply
from ul_house import database
from ul_house.sync import apply, snapshot
from ul_house.sync.errors import RevisionMismatch, SnapshotError, VerificationError

FIXED_NOW = datetime(2026, 10, 5, 12, 30, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    opened = []

    def make(revision, name=None, **kw):
        conn = pf.client_db_at(tmp_path / (name or f"c{len(opened)}.sqlite"), revision, **kw)
        opened.append(conn)
        return conn

    yield make
    for conn in opened:
        conn.close()


@pytest.fixture
def server():
    with LocalServer() as running:
        yield running


def snapshot_blob(tmp_path, n):
    return pf.snapshot_gz_bytes(n, tmp_path / "blobs", extra_icons=0)


def install(conn, tmp_path, n, *, blob=None, sha=None, **kw):
    blob = blob if blob is not None else snapshot_blob(tmp_path, n)
    return snapshot.install_snapshot_blob(conn, blob, sha256=sha or pf.sha256_hex(blob), revision=n, app_version="test",
                                          tmp_dir=tmp_path / "tmp", now=lambda: FIXED_NOW, **kw)


def tmp_files(tmp_path):
    return sorted((tmp_path / "tmp").glob("*")) if (tmp_path / "tmp").exists() else []


def attached(conn):
    return [r[1] for r in conn.execute("PRAGMA database_list")]


def expected_layout(conn, types=True):
    if types:
        return {t: database.table_decl(conn, t) for t in database.game_tables(conn)}
    return {t: database.table_columns(conn, t) for t in database.game_tables(conn)}


def everything(conn):
    return pf.everything(conn, rowid=True)


def test_snapshot_replaces_the_game_tables(db, tmp_path):
    conn = db(1)
    user = pf.dump(conn, pf.USER_TABLES, rowid=True)
    result = install(conn, tmp_path, 3)
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(3)
    assert (result.revision, result.from_revision, result.kind) == (3, 1, "snapshot")
    assert result.counts["equipment"] == len(pf.make_states()[3]["equipment"]) == 7
    assert conn.execute("SELECT revision, from_revision, kind, applied_at, app_version, package_sha256 FROM sync_history "
                        "WHERE revision = 3").fetchone()[:5] == (3, 1, "snapshot", "2026-10-05T12:30:00Z", "test")
    assert database.applied_revision(conn) == 3
    assert "snap" not in attached(conn) and not conn.in_transaction
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert tmp_files(tmp_path) == []
    assert pf.dump(conn, pf.USER_TABLES, rowid=True)["profile"] == user["profile"]


def test_a_fresh_install_has_no_from_revision(db, tmp_path):
    conn = db(None)
    result = install(conn, tmp_path, 4)
    assert result.from_revision is None
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4)
    assert conn.execute("SELECT from_revision, kind FROM sync_history").fetchall() == [(None, "snapshot")]


def test_the_snapshot_file_is_what_p15_says(tmp_path):
    path = pf.build_snapshot(4, tmp_path / "s.sqlite")
    import sqlite3

    c = sqlite3.connect(path)
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('view', 'trigger')").fetchone()[0] == 0
        assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} == set(pf.GAME_TABLES)
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        c.close()
    snapshot.validate_snapshot(path, expected_layout(pf.client_db_at(tmp_path / "c.sqlite", None, user=False)))


# user tables
NO_ALIAS_INVENTORY = ((1, 1, pf.Uid.W1, "a", "b"), (2, 1, pf.Uid.W1, None, None), (3, 1, pf.Uid.M1, "stats1", "stats2"),
                      (4, 2, pf.Uid.W0, None, None))
NO_ALIAS_SAVED = ((1, pf.Uid.W1), (2, pf.Uid.M1))


def test_user_tables_are_untouched_when_no_alias_applies(db, tmp_path):
    conn = db(1, user=False)
    pf.seed_user_data(conn, inventory=NO_ALIAS_INVENTORY, saved=NO_ALIAS_SAVED)
    before = pf.dump(conn, pf.USER_TABLES, rowid=True)
    install(conn, tmp_path, 4)
    assert pf.dump(conn, pf.USER_TABLES, rowid=True) == before
    assert conn.execute("SELECT stat_slot1, stat_slot2 FROM inventory WHERE inv_id = 3").fetchone() == ("stats1", "stats2")


def test_a_user_uid_missing_from_the_snapshot_rolls_everything_back(db, tmp_path):
    conn = db(2)
    conn.execute("INSERT INTO equipment (uid, name, rarity, gear_type, cost, element_id, max_level, entry_kind, keep_reason, "
                 "first_seen, last_seen, last_changed_revision) VALUES ('8888', 'x', 'R', 't', 1, 'none', 1, 'catalog', 'k', "
                 "'x', 'x', 2)")
    conn.execute("INSERT INTO inventory (profile_id, uid) VALUES (1, '8888')")      # snapshot doesnt know 8888 (no alias)
    before = everything(conn)
    with pytest.raises(SnapshotError):
        install(conn, tmp_path, 4)
    assert not conn.in_transaction
    assert everything(conn) == before
    assert "snap" not in attached(conn)
    assert database.applied_revision(conn) == 2
    assert tmp_files(tmp_path) == []


def test_a_user_uid_present_in_the_snapshot_commits(db, tmp_path):
    conn = db(2)
    install(conn, tmp_path, 3)
    assert conn.execute("SELECT count(*) FROM inventory").fetchone()[0] == 5


# alias resolution
def test_alias_in_the_ledger_repoints_user_rows(db, tmp_path):
    conn = db(2)
    install(conn, tmp_path, 4)
    assert conn.execute("SELECT uid, count(*) FROM inventory GROUP BY uid ORDER BY 1").fetchall() == [
        (pf.Uid.W1, 1), (pf.Uid.G2, 3), (pf.Uid.M1, 1)]
    assert conn.execute("SELECT profile_id, uid FROM saved_ref ORDER BY 1, 2").fetchall() == [
        (1, pf.Uid.W1), (1, pf.Uid.G2), (2, pf.Uid.G2)]
    assert conn.execute("SELECT stat_slot1, stat_slot2, note FROM inventory WHERE inv_id = 1").fetchone() == ("atk", "def", "n1")
    assert conn.execute("SELECT count(*) FROM gear_set_slot").fetchone()[0] == 2


def with_ledger(tmp_path, *aliases, n=4):
    """a gzip of snapshot n with extra uid_alias rows (old, new)"""
    base = pf.build_snapshot(n, tmp_path / "base.sqlite")
    values = ", ".join(f"('{old}', '{new}', 2, '{pf.COMMIT}')" for old, new in aliases)
    mutated = pf.mutate_sqlite(base, tmp_path / "mutated.sqlite", f"INSERT INTO uid_alias VALUES {values}")
    return pf.gz(mutated.read_bytes())


def add_equipment(conn, uid):
    conn.execute("INSERT INTO equipment (uid, name, rarity, gear_type, cost, element_id, max_level, entry_kind, keep_reason, "
                 "first_seen, last_seen, last_changed_revision, state, retired_revision) VALUES "
                 f"('{uid}', 'old', 'R', 't', 1, 'none', 1, 'catalog', 'k', 'x', 'x', 2, 'retired', 2)")


def test_a_chain_resolves_to_its_end(db, tmp_path):
    conn = db(2, user=False)
    add_equipment(conn, "2000")
    pf.seed_user_data(conn, inventory=((1, 1, "2000", None, None), (2, 1, pf.Uid.W1, None, None)),
                      saved=((1, "2000"),), sets=False)
    install(conn, tmp_path, 4, blob=with_ledger(tmp_path, ("2000", pf.Uid.G1)))      # 2000 -> 2001 -> 2002
    assert conn.execute("SELECT uid FROM inventory ORDER BY inv_id").fetchall() == [(pf.Uid.G2,), (pf.Uid.W1,)]
    assert conn.execute("SELECT uid FROM saved_ref").fetchall() == [(pf.Uid.G2,)]


def test_a_live_uid_always_wins_over_the_ledger(db, tmp_path):
    conn = db(2, user=False)
    pf.seed_user_data(conn, inventory=((1, 1, pf.Uid.W1, None, None),), saved=((1, pf.Uid.W1),), sets=False)
    install(conn, tmp_path, 4, blob=with_ledger(tmp_path, (pf.Uid.W1, pf.Uid.W0)))
    assert conn.execute("SELECT uid FROM inventory").fetchall() == [(pf.Uid.W1,)]
    assert conn.execute("SELECT uid FROM saved_ref").fetchall() == [(pf.Uid.W1,)]


def test_e1_collision_on_the_snapshot_path(db, tmp_path):
    conn = db(3, user=False)
    pf.seed_user_data(conn, saved=((1, pf.Uid.G1), (1, pf.Uid.G2), (2, pf.Uid.G1)))
    install(conn, tmp_path, 4)
    assert conn.execute("SELECT profile_id, uid FROM saved_ref ORDER BY 1, 2").fetchall() == [
        (1, pf.Uid.G2), (2, pf.Uid.G2)]
    assert conn.execute("SELECT count(*) FROM inventory WHERE uid = ?", (pf.Uid.G1,)).fetchone()[0] == 0


def test_two_old_uids_resolving_to_one_new_uid_do_not_collide(db, tmp_path):
    conn = db(2, user=False)
    add_equipment(conn, "2000")
    pf.seed_user_data(conn, inventory=((1, 1, pf.Uid.W1, None, None),), saved=((1, "2000"), (1, pf.Uid.G1)), sets=False)
    install(conn, tmp_path, 4, blob=with_ledger(tmp_path, ("2000", pf.Uid.G1)))
    assert conn.execute("SELECT profile_id, uid FROM saved_ref").fetchall() == [(1, pf.Uid.G2)]


def test_a_cycle_in_the_ledger_is_an_error_and_changes_nothing(db, tmp_path):
    conn = db(2)
    before = everything(conn)
    with pytest.raises(SnapshotError):
        install(conn, tmp_path, 4, blob=with_ledger(tmp_path, ("7001", "7002"), ("7002", "7001")))
    assert everything(conn) == before and not conn.in_transaction and "snap" not in attached(conn)


def test_alias_resolution_goes_through_apply_inside_the_transaction(db, tmp_path, monkeypatch):
    conn = db(2)
    calls = []
    original = apply.resolve_user_aliases

    def spy(c):
        calls.append((c is conn, c.in_transaction, c.execute("SELECT count(*) FROM equipment").fetchone()[0]))
        return original(c)

    monkeypatch.setattr(apply, "resolve_user_aliases", spy)
    install(conn, tmp_path, 4)
    assert calls == [(True, True, 7)]


def test_wal_readers_see_the_old_revision_until_commit(db, tmp_path, monkeypatch):
    conn = db(1)
    path = tmp_path / "c0.sqlite"
    reader = database.connect(path)
    seen = []
    original = apply.resolve_user_aliases

    def spy(c):
        seen.append((reader.execute("SELECT max(revision) FROM sync_history").fetchone()[0],
                     reader.execute("SELECT count(*) FROM equipment").fetchone()[0]))
        return original(c)

    monkeypatch.setattr(apply, "resolve_user_aliases", spy)
    try:
        install(conn, tmp_path, 4)
        # midtransaction still reads 1
        assert seen == [(1, 5)]
        assert (reader.execute("SELECT max(revision) FROM sync_history").fetchone()[0],
                reader.execute("SELECT count(*) FROM equipment").fetchone()[0]) == (4, 7)
    finally:
        reader.close()


# validation (before attach)
BAD_SNAPSHOTS = {
    "extra_table": ["CREATE TABLE extra_table (a TEXT)"],
    "user_table": ["CREATE TABLE profile (profile_id INTEGER PRIMARY KEY)"],
    "sync_table": ["CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT)"],
    "missing_table": ["DROP TABLE item"],
    "missing_column": ["ALTER TABLE item DROP COLUMN name"],
    "extra_column": ["ALTER TABLE item ADD COLUMN extra TEXT"],
    "column_type": ["CREATE TABLE item_new (uid TEXT PRIMARY KEY, name INTEGER NOT NULL)",
                    "INSERT INTO item_new SELECT uid, name FROM item", "DROP TABLE item", "ALTER TABLE item_new RENAME TO item"],
    "view": ["CREATE VIEW v AS SELECT 1 AS a"],
    "trigger": ["CREATE TRIGGER t AFTER INSERT ON item BEGIN SELECT 1; END"],
    "wal_mode": ["PRAGMA journal_mode = WAL"],
}


@pytest.mark.parametrize("variant", sorted(BAD_SNAPSHOTS))
def test_a_bad_snapshot_is_rejected_before_attach(db, tmp_path, variant):
    base = pf.build_snapshot(4, tmp_path / "base.sqlite")
    bad = pf.mutate_sqlite(base, tmp_path / "bad.sqlite", *BAD_SNAPSHOTS[variant])
    if variant == "wal_mode":
        import sqlite3

        probe = sqlite3.connect(bad)
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "the fixture must really be a WAL file"
        probe.close()
    conn = db(1)
    before = everything(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    with pytest.raises(SnapshotError):
        install(conn, tmp_path, 4, blob=pf.gz(bad.read_bytes()))
    conn.set_trace_callback(None)
    assert not any("ATTACH" in s.upper() for s in statements), "validation must precede ATTACH"
    assert everything(conn) == before and "snap" not in attached(conn) and not conn.in_transaction
    assert tmp_files(tmp_path) == []
    # and the validator alone says the same
    with pytest.raises(SnapshotError):
        snapshot.validate_snapshot(bad, expected_layout(conn))


def test_column_types_are_checked_only_when_given(db, tmp_path):
    base = pf.build_snapshot(4, tmp_path / "base.sqlite")
    bad = pf.mutate_sqlite(base, tmp_path / "bad.sqlite", *BAD_SNAPSHOTS["column_type"])
    conn = db(1, user=False)
    with pytest.raises(SnapshotError):
        snapshot.validate_snapshot(bad, expected_layout(conn, types=True))
    snapshot.validate_snapshot(base, expected_layout(conn, types=False))
    snapshot.validate_snapshot(base, expected_layout(conn, types=True))


def test_garbage_and_truncated_files_are_rejected(db, tmp_path):
    conn = db(1)
    before = everything(conn)
    good = pf.build_snapshot(4, tmp_path / "good.sqlite").read_bytes()
    for name, content in (("not a database", b"this is not a sqlite file" * 100), ("truncated", good[: len(good) // 3]),
                          ("empty", b"")):
        with pytest.raises(SnapshotError):
            install(conn, tmp_path, 4, blob=pf.gz(content))
        assert everything(conn) == before, name
        assert tmp_files(tmp_path) == [], name


def test_sha256_mismatch_is_refused_before_anything_is_written(db, tmp_path):
    conn = db(1)
    before = everything(conn)
    blob = snapshot_blob(tmp_path, 4)
    with pytest.raises(VerificationError):
        install(conn, tmp_path, 4, blob=blob, sha="0" * 64)
    with pytest.raises(VerificationError):
        install(conn, tmp_path, 4, blob=blob + b"x", sha=pf.sha256_hex(blob))
    assert tmp_files(tmp_path) == [] and everything(conn) == before


def test_row_counts_must_match_the_manifest(db, tmp_path):
    conn = db(1)
    counts = dict(pf.entity_counts(4))
    install(conn, tmp_path, 4, entity_counts=counts)
    assert database.applied_revision(conn) == 4
    conn2 = db(1)
    before2 = everything(conn2)
    counts["equipment"] += 1
    with pytest.raises(SnapshotError):
        install(conn2, tmp_path, 4, entity_counts=counts)
    assert everything(conn2) == before2


# revisions / repairs
def test_a_snapshot_that_is_not_newer_is_refused(db, tmp_path):
    conn = db(3)
    before = everything(conn)
    for n in (3, 2, 1):
        with pytest.raises(RevisionMismatch):
            install(conn, tmp_path, n)
        assert everything(conn) == before


def test_repair_reapplies_the_same_revision_and_updates_its_history_row(db, tmp_path):
    conn = db(1)
    package = pf.load_package(pf.make_package(1, 3))
    apply.apply_package(conn, package[0], package_sha256=package[1], app_version="old", now=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert conn.execute("SELECT kind FROM sync_history WHERE revision = 3").fetchone() == ("update",)
    # corrupt local
    conn.execute("UPDATE equipment SET cost = 999")
    conn.execute("DELETE FROM stat")
    install(conn, tmp_path, 3, repair=True)
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(3)
    assert conn.execute("SELECT revision, from_revision, kind, applied_at, app_version FROM sync_history WHERE revision = 3"
                        ).fetchall() == [(3, 1, "snapshot", "2026-10-05T12:30:00Z", "test")]
    assert conn.execute("SELECT count(*) FROM sync_history").fetchone()[0] == 2
    with pytest.raises(RevisionMismatch):
        # forward repair
        install(conn, tmp_path, 2, repair=True)


def test_the_snapshot_file_is_only_read_and_is_deleted(db, tmp_path):
    conn = db(1)
    path = pf.build_snapshot(4, tmp_path / "keep.sqlite")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = snapshot.apply_snapshot(conn, path, revision=4, package_sha256="0" * 64, app_version="t")
    assert result.kind == "snapshot" and "snap" not in attached(conn)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before                     # mode=ro: not a byte changed
    assert not any(path.parent.glob("keep.sqlite-*"))
    # the downloaded temp file is removed after success and after failure
    conn2 = db(1)
    install(conn2, tmp_path, 4)
    assert tmp_files(tmp_path) == []
    conn3 = db(1)
    with pytest.raises(SnapshotError):
        install(conn3, tmp_path, 4, blob=pf.gz(b"junk"))
    assert tmp_files(tmp_path) == []


def test_snapshot_plus_package_equals_the_later_snapshot(db, tmp_path):
    for r in (1, 2, 3):
        conn = db(None)
        install(conn, tmp_path, r)
        package, sha = pf.load_package(pf.make_package(r, 4))
        apply.apply_package(conn, package, package_sha256=sha, app_version="t")
        assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4), r
        assert database.applied_revision(conn) == 4


def test_bundled_snapshot_installs_on_first_launch(db, tmp_path):
    conn = db(None)
    bundled = tmp_path / "unison-4.sqlite.gz"
    bundled.write_bytes(snapshot_blob(tmp_path, 4))
    result = snapshot.install_bundled_snapshot(conn, bundled, revision=4, app_version="1.0", tmp_dir=tmp_path / "tmp")
    assert (result.revision, result.from_revision, result.kind) == (4, None, "snapshot")
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4)
    assert conn.execute("SELECT kind, app_version, package_sha256 FROM sync_history").fetchall() == [
        ("snapshot", "1.0", pf.sha256_hex(bundled.read_bytes()))]
    assert tmp_files(tmp_path) == []


def test_a_stale_client_takes_the_snapshot_without_asking_for_a_package(db, tmp_path, server):
    pub = pf.build_pub(tmp_path / "p", minimum_revision=3)
    pf.serve(pub, server)
    conn = db(1)
    from ul_house.sync import service

    result = pf.run_sync(conn, pf.make_client(server), pub, tmp_path)
    assert result.outcome is service.Outcome.SNAPSHOT
    assert server.hits("/v1/updates/1") == []
    assert len(server.hits("/v1/snapshots/4")) == 1
    assert conn.execute("SELECT revision, from_revision, kind FROM sync_history WHERE revision = 4").fetchone() == (4, 1, "snapshot")
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4)
    assert conn.execute("SELECT count(*) FROM inventory").fetchone()[0] == 5


@pytest.mark.parametrize("reason", ["below_minimum", "package_missing"])
def test_requires_snapshot_from_the_api_takes_the_snapshot_path(db, tmp_path, server, reason):
    pub = pf.build_pub(tmp_path / "p")
    pf.serve(pub, server)
    server.route("/v1/updates/1", lambda request: pf.requires_snapshot_reply(4, 1, reason))
    conn = db(1)
    from ul_house.sync import service

    result = pf.run_sync(conn, pf.make_client(server), pub, tmp_path)
    assert result.outcome is service.Outcome.SNAPSHOT
    assert len(server.hits("/v1/updates/1")) == 1 and len(server.hits("/v1/snapshots/4")) == 1
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4)


def test_a_fresh_install_takes_the_snapshot_path(db, tmp_path, server):
    pub = pf.build_pub(tmp_path / "p")
    pf.serve(pub, server)
    conn = db(None)
    from ul_house.sync import service

    result = pf.run_sync(conn, pf.make_client(server), pub, tmp_path)
    assert result.outcome is service.Outcome.SNAPSHOT and result.local_revision == 4
    assert [h.path for h in server.log if h.path != "/v1/version"][:2] == ["/v1/manifests/4", "/v1/snapshots/4"]
    assert not [h for h in server.log if h.path.startswith("/v1/updates")]
    assert conn.execute("SELECT from_revision, kind FROM sync_history").fetchall() == [(None, "snapshot")]


def test_a_corrupt_snapshot_download_changes_nothing_and_leaves_no_file(db, tmp_path, server):
    pub = pf.build_pub(tmp_path / "p", minimum_revision=4, snapshot_only=True)
    pf.serve(pub, server)
    blob = bytearray(pub.snapshot_gz())
    blob[len(blob) // 2] ^= 0xFF
    server.route("/v1/snapshots/4", Reply(200, bytes(blob), {"Content-Type": "application/gzip", "X-API-Commit": "dev"}))
    conn = db(1)
    before = everything(conn)
    from ul_house.sync import service

    result = pf.run_sync(conn, pf.make_client(server), pub, tmp_path)
    assert result.outcome is service.Outcome.FAILED
    assert everything(conn) == before and tmp_files(tmp_path) == []


def test_a_bundle_directory_installs_on_first_launch(db, tmp_path):
    conn = db(None)
    bundle = pf.make_bundle(tmp_path / "bundle", 4)
    result = snapshot.install_bundled_snapshot(conn, bundle, app_version="1.0", tmp_dir=tmp_path / "tmp")
    assert (result.revision, result.from_revision, result.kind) == (4, None, "snapshot")
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4)
    assert conn.execute("SELECT revision, kind, app_version, package_sha256 FROM sync_history").fetchall() == [
        (4, "snapshot", "1.0", pf.sha256_hex((bundle / "unison-4.sqlite.gz").read_bytes()))]
    assert tmp_files(tmp_path) == []
    # the revision argument may be given when it agrees; a different one is refused
    other = db(None)
    snapshot.install_bundled_snapshot(other, bundle, revision=4, app_version="1.0", tmp_dir=tmp_path / "tmp")
    third = db(None)
    with pytest.raises(SnapshotError):
        snapshot.install_bundled_snapshot(third, bundle, revision=3, app_version="1.0", tmp_dir=tmp_path / "tmp")
    assert database.applied_revision(third) is None


BAD_BUNDLES = {
    "no_bundle_json": lambda d: (d / "bundle.json").unlink(),
    "bad_json": lambda d: (d / "bundle.json").write_text("{not json"),
    "empty_json": lambda d: (d / "bundle.json").write_text(""),
    "not_an_object": lambda d: (d / "bundle.json").write_text("[4, \"unison-4.sqlite.gz\"]"),
    "missing_revision": lambda d: (d / "bundle.json").write_text('{"file": "unison-4.sqlite.gz"}'),
    "missing_file_key": lambda d: (d / "bundle.json").write_text('{"revision": 4}'),
    "bool_revision": lambda d: (d / "bundle.json").write_text('{"revision": true, "file": "unison-1.sqlite.gz"}'),
    "string_revision": lambda d: (d / "bundle.json").write_text('{"revision": "4", "file": "unison-4.sqlite.gz"}'),
    "name_revision_mismatch": lambda d: (d / "bundle.json").write_text('{"revision": 4, "file": "unison-3.sqlite.gz"}'),
    "path_traversal": lambda d: (d / "bundle.json").write_text('{"revision": 4, "file": "../unison-4.sqlite.gz"}'),
    "absolute_path": lambda d: (d / "bundle.json").write_text('{"revision": 4, "file": "/etc/unison-4.sqlite.gz"}'),
    "subdirectory": lambda d: (d / "bundle.json").write_text('{"revision": 4, "file": "sub/unison-4.sqlite.gz"}'),
    "trailing_newline": lambda d: (d / "bundle.json").write_text('{"revision": 4, "file": "unison-4.sqlite.gz\\n"}'),
    "other_file_name": lambda d: (d / "bundle.json").write_text('{"revision": 4, "file": "snapshot.sqlite.gz"}'),
    "missing_file": lambda d: (d / "unison-4.sqlite.gz").unlink(),
}


@pytest.mark.parametrize("variant", sorted(BAD_BUNDLES))
def test_a_bad_bundle_is_refused_and_changes_nothing(db, tmp_path, variant):
    bundle = pf.make_bundle(tmp_path / "bundle", 4)
    (tmp_path / "unison-4.sqlite.gz").write_bytes(b"decoy outside the bundle")
    BAD_BUNDLES[variant](bundle)
    conn = db(None)
    before = everything(conn)
    with pytest.raises((SnapshotError, OSError)) as info:
        snapshot.install_bundled_snapshot(conn, bundle, app_version="1.0", tmp_dir=tmp_path / "tmp")
    if variant != "missing_file":
        assert isinstance(info.value, SnapshotError), repr(info.value)
    assert everything(conn) == before and not conn.in_transaction and tmp_files(tmp_path) == []


def test_a_bundle_file_that_is_not_a_snapshot_is_refused(db, tmp_path):
    bundle = pf.make_bundle(tmp_path / "bundle", 4, write_file=False)
    (bundle / "unison-4.sqlite.gz").write_bytes(pf.gz(b"not sqlite"))
    conn = db(None)
    with pytest.raises(SnapshotError):
        snapshot.install_bundled_snapshot(conn, bundle, app_version="1.0", tmp_dir=tmp_path / "tmp")
    assert database.applied_revision(conn) is None and tmp_files(tmp_path) == []


def test_a_bundle_then_an_update_then_sync(db, tmp_path, server):
    """first launch from the bundle (revision 2), then the network brings it to 4"""
    conn = db(None)
    snapshot.install_bundled_snapshot(conn, pf.make_bundle(tmp_path / "bundle", 2), app_version="1.0", tmp_dir=tmp_path / "tmp")
    pub = pf.build_pub(tmp_path / "p")
    pf.serve(pub, server)
    from ul_house.sync import service

    assert pf.run_sync(conn, pf.make_client(server), pub, tmp_path).outcome is service.Outcome.UPDATED
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4)
