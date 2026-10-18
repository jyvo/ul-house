"""client database: bootstrap, migrations, settings"""
import sqlite3
import threading

import pytest

import pub_fixture as pf
from ul_house import database
from ul_house.database import entities


def tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def structure(conn) -> dict:
    out = {}
    for name, kind in conn.execute("SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY 1"):
        entry = {"type": kind}
        if kind == "table":
            entry["columns"] = [tuple(r[1:]) for r in conn.execute(f'PRAGMA table_xinfo("{name}")')]
            entry["fks"] = sorted(tuple(r[2:]) for r in conn.execute(f'PRAGMA foreign_key_list("{name}")'))
            entry["indexes"] = sorted(
                (r[1], r[2], r[3], tuple(c[2] for c in conn.execute(f'PRAGMA index_info("{r[1]}")')))
                for r in conn.execute(f'PRAGMA index_list("{name}")') if not r[1].startswith("sqlite_autoindex")
            ) + sorted(
                (r[2], tuple(c[2] for c in conn.execute(f'PRAGMA index_info("{r[1]}")')))
                for r in conn.execute(f'PRAGMA index_list("{name}")') if r[1].startswith("sqlite_autoindex")
            )
        out[name] = entry
    return out


@pytest.fixture
def conn(tmp_path):
    connection = database.open_database(tmp_path / "ul.sqlite")
    yield connection
    connection.close()


def test_fresh_file_is_created_from_schema_sql(conn):
    assert tables(conn) == set(entities.GAME_TABLES + entities.LEDGER_TABLES + entities.SYNC_TABLES + entities.USER_TABLES)
    assert tables(conn) == {t for section in ("game", "ledger", "sync", "user") for t in entities.section_tables(section)}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION == 1
    assert conn.execute("PRAGMA application_id").fetchone()[0] == database.APPLICATION_ID
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert views == {"gear_set_status", "stat_assignable"}
    assert database.schema_version(conn) == 1


def test_table_sets_are_the_four_sections():
    assert database.USER_TABLES == entities.section_tables("user")
    assert database.SYNC_TABLES == entities.section_tables("sync")
    assert set(database.GAME_TABLES) | set(database.LEDGER_TABLES) == set(pf.GAME_TABLES)
    assert set(pf.LOAD_ORDER) == set(pf.GAME_TABLES)


def test_connect_settings(tmp_path):
    c = database.connect(tmp_path / "x.sqlite")
    try:
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == database.BUSY_TIMEOUT_MS == 5000
        assert c.isolation_level is None
        assert not c.in_transaction
        errors = []

        def other_thread():
            try:
                c.execute("SELECT 1")
            except sqlite3.ProgrammingError as exc:
                errors.append(exc)

        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        assert len(errors) == 1
    finally:
        c.close()


def test_baseline_is_a_verbatim_copy_of_schema_sql():
    baseline = (database.MIGRATIONS_DIR / "0001_baseline.sql").read_bytes()
    assert baseline == entities.SCHEMA_PATH.read_bytes()
    assert database.MIGRATIONS[0].version == 1 and database.MIGRATIONS[0].name == "baseline"


def test_baseline_plus_migrations_equals_schema_sql(tmp_path, conn):
    other = sqlite3.connect(tmp_path / "migrated.sqlite", isolation_level=None)
    other.execute("PRAGMA foreign_keys = ON")
    try:
        assert database.migrate(other, database.MIGRATIONS) == database.SCHEMA_VERSION
        assert structure(other) == structure(conn)
    finally:
        other.close()


def test_migration_runner_applies_once_and_bumps_the_version(conn):
    add_note = database.Migration(version=2, name="profile_note", sql="ALTER TABLE profile ADD COLUMN note TEXT;")
    migrations = database.MIGRATIONS + (add_note,)
    assert database.ensure_schema(conn, migrations=migrations) == 2
    assert database.schema_version(conn) == 2
    assert "note" in [r[1] for r in conn.execute("PRAGMA table_info(profile)")]
    assert database.ensure_schema(conn, migrations=migrations) == 2
    assert [r[1] for r in conn.execute("PRAGMA table_info(profile)")].count("note") == 1
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_failing_migration_rolls_back(conn):
    bad = database.Migration(version=2, name="broken",
                             sql="ALTER TABLE profile ADD COLUMN extra TEXT;\nINSERT INTO no_such_table VALUES (1);")
    with pytest.raises(database.MigrationError):
        database.ensure_schema(conn, migrations=database.MIGRATIONS + (bad,))
    assert database.schema_version(conn) == 1
    assert "extra" not in [r[1] for r in conn.execute("PRAGMA table_info(profile)")]
    assert not conn.in_transaction
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_migration_that_breaks_foreign_keys_is_rolled_back(conn):
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO profile (profile_id, name) VALUES (1, 'a')")
    conn.execute("INSERT INTO inventory (profile_id, uid) VALUES (1, 'no-such-uid')")
    conn.execute("PRAGMA foreign_keys = ON")
    noop = database.Migration(version=2, name="noop", sql="CREATE TABLE IF NOT EXISTS scratch (a INTEGER);")
    with pytest.raises(database.MigrationError):
        database.ensure_schema(conn, migrations=database.MIGRATIONS + (noop,))
    assert database.schema_version(conn) == 1
    assert "scratch" not in tables(conn)


def test_gaps_and_duplicates_in_migration_numbers(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(database.MigrationError):
        database.load_migrations(tmp_path)
    (tmp_path / "0003_c.sql").unlink()
    (tmp_path / "0001_b.sql").write_text("SELECT 1;")
    with pytest.raises(database.MigrationError):
        database.load_migrations(tmp_path)
    (tmp_path / "0001_b.sql").unlink()
    (tmp_path / "0002_b.sql").write_text("SELECT 1;")
    assert [m.version for m in database.load_migrations(tmp_path)] == [1, 2]


def test_newer_and_foreign_files_are_refused(tmp_path):
    newer = tmp_path / "newer.sqlite"
    c = database.open_database(newer)
    c.execute(f"PRAGMA user_version = {database.SCHEMA_VERSION + 1}")
    c.close()
    c = database.connect(newer)
    try:
        with pytest.raises(database.SchemaTooNew):
            database.ensure_schema(c)
        assert database.schema_version(c) == database.SCHEMA_VERSION + 1
    finally:
        c.close()

    foreign = tmp_path / "foreign.sqlite"
    raw = sqlite3.connect(foreign)
    raw.execute("CREATE TABLE somebody_elses (a)")
    raw.commit()
    raw.close()
    with pytest.raises(database.NotOurDatabase):
        database.open_database(foreign)
    other_app = tmp_path / "other_app.sqlite"
    raw = sqlite3.connect(other_app)
    raw.execute("CREATE TABLE t (a)")
    raw.execute("PRAGMA application_id = 12345")
    raw.commit()
    raw.close()
    with pytest.raises(database.NotOurDatabase):
        database.open_database(other_app)


def test_a_crash_during_create_leaves_an_empty_file(tmp_path):
    path = tmp_path / "crash.sqlite"
    c = database.connect(path)
    with pytest.raises(sqlite3.Error):
        database.create(c, entities.SCHEMA_PATH.read_text() + "\nTHIS IS NOT SQL;\n", 1)
    assert not c.in_transaction
    assert tables(c) == set()
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0
    assert c.execute("PRAGMA application_id").fetchone()[0] == 0
    c.close()
    again = database.open_database(path)          # next launch creates it again
    try:
        assert "equipment" in tables(again) and database.schema_version(again) == 1
    finally:
        again.close()


def test_game_tables_are_parent_first_and_are_the_shipped_set(conn):
    order = database.game_tables(conn)
    assert set(order) == set(entities.GAME_TABLES) | set(entities.LEDGER_TABLES)       # game + ledger
    assert not set(order) & (set(database.USER_TABLES) | set(database.SYNC_TABLES))
    assert len(order) == len(set(order)) == 24
    position = {t: i for i, t in enumerate(order)}
    for table in order:
        for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
            parent = fk[2]
            if parent != table:
                assert position[parent] < position[table], f"{table} references {parent}, which comes later"


def test_column_helpers(conn):
    assert database.table_columns(conn, "item") == ("uid", "name")
    assert database.table_columns(conn, "equipment")[:3] == ("uid", "name", "rarity")
    assert database.table_decl(conn, "item") == (("uid", "TEXT"), ("name", "TEXT"))
    decl = dict(database.table_decl(conn, "equipment"))
    assert decl["cost"] == "INTEGER" and decl["uid"] == "TEXT"
    assert set(decl.values()) == {"TEXT", "INTEGER"}
    assert database.primary_key(conn, "equipment") == ("uid",)
    assert database.primary_key(conn, "stat") == ("uid", "label", "tier")
    assert database.primary_key(conn, "evolution_material") == ("kind", "from_uid", "to_uid", "ordinal")
    assert database.integer_key(conn, "proc", "proc_id") is True
    assert database.integer_key(conn, "equipment", "uid") is False
    assert database.integer_key(conn, "icon", "sha256") is False


def test_applied_revision_and_state(conn):
    assert database.applied_revision(conn) is None
    for revision in (3, 7, 5):
        conn.execute("INSERT INTO sync_history (revision, from_revision, kind, applied_at, app_version, package_sha256) "
                     "VALUES (?, NULL, 'snapshot', '2026-10-01T00:00:00Z', 't', ?)", (revision, "0" * 64))
    assert database.applied_revision(conn) == 7
    assert database.get_state(conn, "current_etag") is None
    database.set_state(conn, {"current_etag": '"a"', "expires_at": "2026-10-18T00:00:00Z"})
    assert database.get_state(conn, "current_etag") == '"a"'
    database.set_state(conn, {"current_etag": '"b"'})
    assert database.get_state(conn, "current_etag") == '"b"'
    assert database.get_state(conn, "expires_at") == "2026-10-18T00:00:00Z"
    assert not conn.in_transaction
    assert not any(r[0] == "revision" for r in conn.execute("SELECT key FROM sync_state"))


def test_user_foreign_keys_are_deferred_and_game_foreign_keys_are_not(conn):
    conn.execute("INSERT INTO profile (profile_id, name) VALUES (1, 'a')")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO inventory (profile_id, uid) VALUES (1, 'no-such-uid')")
    conn.execute("INSERT INTO saved_ref (profile_id, uid) VALUES (1, 'no-such-uid')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("COMMIT")
    assert conn.in_transaction
    conn.execute("ROLLBACK")
    assert conn.execute("SELECT count(*) FROM inventory").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO weapon (uid) VALUES ('missing')")
    conn.execute("INSERT INTO element (element_id) VALUES ('none')")
    conn.execute("INSERT INTO equipment (uid, name, rarity, gear_type, cost, element_id, max_level, entry_kind, "
                 "keep_reason, first_seen, last_seen, last_changed_revision) VALUES "
                 "('1', 'n', 'R', 't', 1, 'none', 1, 'catalog', 'k', 'x', 'x', 1)")
    conn.execute("INSERT INTO inventory (profile_id, uid) VALUES (1, '1')")
    assert conn.execute("SELECT count(*) FROM inventory").fetchone()[0] == 1


def test_equipment_element_is_not_null(conn):
    decl = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(equipment)")}
    assert decl["element_id"] == 1
    assert decl["rarity"] == decl["gear_type"] == decl["cost"] == decl["max_level"] == 1


def test_views_work_with_trusted_schema_off(tmp_path):
    c = pf.client_db_at(tmp_path / "v.sqlite", 2)
    try:
        assert c.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        rows = c.execute("SELECT set_id, slot_count, total_cost, retired_count, over_cap FROM gear_set_status").fetchall()
        assert rows == [(1, 2, 15 + 20, 1, 1)]
        assert c.execute("SELECT count(*) FROM stat_assignable").fetchone()[0] > 0
    finally:
        c.close()


def test_fixture_loads_every_revision(tmp_path):
    for revision in (1, 2, 3, 4):
        c = pf.client_db_at(tmp_path / f"r{revision}.sqlite", revision)
        try:
            assert database.applied_revision(c) == revision
            assert pf.dump(c, pf.GAME_TABLES) == pf.expected_dump(revision)
            assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            c.close()
