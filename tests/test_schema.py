import re
import sqlite3
from pathlib import Path

import pytest

from catalog_fixtures import CATALOG
from ul_house import ids
from ul_house.database import entities
from ul_house.database.entities import (
    DATASET_SCHEMA_VERSION,
    ENTITIES,
    entity,
    key_from_text,
    key_to_text,
    owner,
    schema_sections,
    section_tables,
)

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "src" / "ul_house" / "database" / "schema.sql"
SCHEMA = SCHEMA_FILE.read_text(encoding="utf-8")

SECTION_NAMES = ("game", "ledger", "sync", "user")
GAME_TABLES = {
    "element", "element_relation", "item", "icon", "skill_effect", "proc_family", "proc",
    "proc_condition", "proc_scaling", "weapon_ability", "passive_skill", "equipment", "weapon",
    "defensive_gear", "monster", "monster_skill", "potential_level", "effect_link", "stat",
    "evolution_edge", "evolution_chain", "evolution_material",
}
LEDGER_TABLES = {"uid_retired", "uid_alias"}
SYNC_TABLES = {"sync_history", "sync_state"}
USER_TABLES = {"profile", "inventory", "saved_ref", "gear_set", "gear_set_slot"}
INVENTORY = {
    "game": GAME_TABLES,
    "ledger": LEDGER_TABLES,
    "sync": SYNC_TABLES,
    "user": USER_TABLES,
}
VIEWS = {"gear_set_status", "stat_assignable"}
OWNER_KINDS = {"proc", "weapon_ability", "monster_skill", "passive_skill", "potential_level"}
EVO_KINDS = ("reforge", "awakening", "enlightening")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


# helpers
def connect(sql: str | None = None, path=":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA if sql is None else sql)
    return conn


def table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def view_names(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}


def sql_statements(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("--"):
            continue
        out.append(re.sub(r"\s--.*$", "", line))
    return "\n".join(out)


def add_equipment(conn, uid, *, cost=40, state="live", entry_kind="catalog", element="fire", name=None):
    conn.execute(
        "INSERT INTO equipment (uid, name, rarity, gear_type, cost, element_id, max_level, "
        "entry_kind, keep_reason, state, first_seen, last_seen, last_changed_revision) "
        "VALUES (?, ?, 'ur', 'sword', ?, ?, 100, ?, 'in_scope', ?, "
        "'2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', 1)",
        (uid, name or f"gear {uid}", cost, element, entry_kind, state),
    )


def add_elements(conn):
    conn.execute("INSERT INTO element (element_id) VALUES ('fire'), ('water')")


def full_db() -> sqlite3.Connection:
    """one row in every table. exercises every FK target and every aggregate predicate"""
    conn = connect()
    conn.execute("INSERT INTO element VALUES ('fire'), ('water')")
    conn.execute("INSERT INTO element_relation VALUES ('fire', 'water', 'weakness')")
    conn.execute("INSERT INTO element_relation VALUES ('water', 'fire', 'effective')")
    conn.execute("INSERT INTO item VALUES ('5864', 'sword slate')")
    conn.executemany(
        "INSERT INTO icon (sha256, kind, bytes) VALUES (?, ?, ?)",
        [("a" * 64, "equipment", 123), ("b" * 64, "item", 45), ("c" * 64, "ability", 7)],
    )
    e1 = ids.effect_id(None, "recovers 10 cost.")
    e2 = ids.effect_id("yourself", "fills unison gauge by 4.")
    e3 = ids.effect_id("all allies", "increases all stats by 200%.")
    conn.executemany(
        "INSERT INTO skill_effect (effect_id, target, description) VALUES (?, ?, ?)",
        [
            (e1, None, "recovers 10 cost."),
            (e2, "yourself", "fills unison gauge by 4."),
            (e3, "all allies", "increases all stats by 200%."),
        ],
    )
    fam = ids.family_id("dragon slayer")
    conn.execute("INSERT INTO proc_family VALUES (?, 'dragon slayer')", (fam,))
    eh = ids.effect_hash([(None, "recovers 10 cost.")])
    proc = ids.proc_id("water dragon slayer xl", eh)
    conn.execute(
        "INSERT INTO proc (proc_id, family_id, element_id, size, raw_name, activation_rate, "
        "element_position) VALUES (?, ?, 'water', 'xl', 'water dragon slayer xl', 'l', 'infix')",
        (proc, fam),
    )
    conn.execute("INSERT INTO proc_condition VALUES (?, 1, 'when hit.')", (proc,))
    conn.execute(
        "INSERT INTO proc_scaling (proc_id, ordinal, scale_kind, element_id, gear_kind, cap, "
        "pieces_needed, description) VALUES (?, 1, 'ability_power', 'water', 'weapons', '5XL+', 5, 'scales.')",
        (proc,),
    )
    conn.execute("INSERT INTO weapon_ability VALUES ('2130', 'valiant blade')")
    passive = ids.passive_id("eclipse's blessing", eh)
    conn.execute(
        "INSERT INTO passive_skill VALUES (?, ?, ?)", (passive, "eclipse's blessing", eh)
    )
    for uid in ("1001", "1002", "1003"):
        add_equipment(conn, uid)
    conn.execute("INSERT INTO weapon (uid, infusion_count, proc_id, ability_uid) VALUES ('1001', 5, ?, '2130')", (proc,))
    conn.execute("INSERT INTO defensive_gear (uid, infusion_count, proc_id) VALUES ('1002', 3, ?)", (proc,))
    conn.execute("INSERT INTO monster (uid, passive_id, restrictions) VALUES ('1003', ?, 'fodder')", (passive,))
    skill = ids.skill_id("1003", 1)
    conn.execute("INSERT INTO monster_skill VALUES (?, '1003', 1, 'skill one')", (skill,))
    conn.execute("INSERT INTO potential_level VALUES ('1003', 1, ?)", (e2,))
    conn.executemany(
        "INSERT INTO effect_link (owner_kind, owner_id, ordinal, effect_id) VALUES (?, ?, ?, ?)",
        [
            ("proc", ids.id_to_text(proc), 1, e1),
            ("weapon_ability", "2130", 1, e2),
            ("monster_skill", ids.id_to_text(skill), 1, e3),
            ("passive_skill", ids.id_to_text(passive), 1, e1),
            ("potential_level", "1003:1", 1, e2),
        ],
    )
    conn.executemany(
        "INSERT INTO stat (uid, label, tier, value) VALUES (?, ?, ?, ?)",
        [("1001", "atk", "initial", 100), ("1003", "stats1", "max", 200)],
    )
    conn.executemany(
        "INSERT INTO evolution_edge (kind, from_uid, to_uid, evidence, from_name, to_name) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("reforge", "1001", "9999", "both", "gear 1001", "ghost gear"),
            ("awakening", "1002", "1001", "after", "gear 1002", "gear 1001"),
            ("reforge", "9998", "1003", "before", "older ghost", "gear 1003"),
        ],
    )
    conn.executemany(
        "INSERT INTO evolution_chain (kind, chain_id, uid, position) VALUES (?, ?, ?, ?)",
        [("reforge", "1001", "1001", 0), ("reforge", "1001", "1002", 1), ("reforge", "1003", "1003", 0)],
    )
    conn.executemany(
        "INSERT INTO evolution_material (kind, from_uid, to_uid, ordinal, material_kind, ref_uid, "
        "ref_name, quantity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("reforge", "1001", "9999", 1, "gear", "5000", "material gear", 1),
            ("reforge", "1001", "9999", 2, "item", "5864", "sword slate", 3),
            ("reforge", "9998", "1003", 1, "gear", None, "nameless gear", 1),
        ],
    )
    conn.execute("INSERT INTO uid_retired VALUES ('9000', 5, 'gone')")
    conn.execute("INSERT INTO uid_alias VALUES ('9001', '1001', 6, 'abc123')")
    conn.execute(
        "INSERT INTO sync_history VALUES (1, NULL, 'snapshot', '2026-01-01T00:00:00Z', '0.1.0', 'sha')"
    )
    conn.execute(
        "INSERT INTO sync_history VALUES (2, 1, 'update', '2026-01-02T00:00:00Z', '0.1.0', 'sha2')"
    )
    conn.execute("INSERT INTO sync_state VALUES ('etag', 'abc')")
    conn.execute("INSERT INTO profile (profile_id, name) VALUES (1, 'main'), (2, 'alt')")
    conn.executemany(
        "INSERT INTO inventory (inv_id, profile_id, uid) VALUES (?, ?, ?)",
        [(1, 1, "1001"), (2, 1, "1002"), (3, 2, "1001")],
    )
    conn.execute("INSERT INTO saved_ref (profile_id, uid) VALUES (1, '1003')")
    conn.execute("INSERT INTO gear_set (set_id, profile_id, name, cost_cap) VALUES (1, 1, 'set', 80)")
    conn.execute("INSERT INTO gear_set_slot VALUES (1, 1, 1, 1), (1, 2, 1, 2)")
    return conn


@pytest.fixture
def db():
    conn = connect()
    yield conn
    conn.close()


@pytest.fixture
def full():
    conn = full_db()
    yield conn
    conn.close()


@pytest.fixture
def base(db):
    """parents for the single-row CHECK tests"""
    add_elements(db)
    db.execute("INSERT INTO proc_family VALUES (1, 'fam')")
    db.execute("INSERT INTO skill_effect VALUES (1, NULL, 'd')")
    add_equipment(db, "e1")
    return db


def dump(conn) -> dict[str, list]:
    out = {}
    for table in sorted(table_names(conn)):
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        out[table] = sorted(rows, key=repr)
    return out


# apply file
def test_applies_to_fresh_database(db):
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    expected = set().union(*INVENTORY.values())
    assert table_names(db) == expected


def test_applies_twice(db):
    before = dump(db)
    schema_before = db.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    db.executescript(SCHEMA)
    assert db.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema_before
    assert dump(db) == before


def test_sections_in_order():
    assert re.findall(r"^-- @section (\w+)$", SCHEMA, flags=re.M) == list(SECTION_NAMES)
    preamble = re.split(r"^-- @section \w+$", SCHEMA, flags=re.M)[0]
    assert sql_statements(preamble).strip() == ""
    assert re.search(r"dataset_schema_version:\s*1\b", SCHEMA)
    code = sql_statements(SCHEMA)
    for line in code.splitlines():
        assert not re.match(r"^\s*(PRAGMA|BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", line, re.I), line
    assert "user_version" not in code.lower()


def test_types_are_text_or_integer(db):
    for table in GAME_TABLES | LEDGER_TABLES:
        for _cid, name, ctype, *_ in db.execute(f"PRAGMA table_xinfo({table})"):
            assert ctype in ("TEXT", "INTEGER"), f"{table}.{name}: {ctype}"


def test_views_exist(db):
    assert view_names(db) == VIEWS
    for view in VIEWS:
        db.execute(f"SELECT * FROM {view}").fetchall()


@pytest.mark.parametrize("section", SECTION_NAMES)
def test_tables_per_section(section):
    assert set(section_tables(section)) == INVENTORY[section]
    assert len(section_tables(section)) == len(INVENTORY[section])


def test_schema_sections():
    sections = schema_sections()
    assert tuple(sections) == SECTION_NAMES == entities.SECTIONS
    for name, sql in sections.items():
        found = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))
        assert found == INVENTORY[name], name
    assert "UL House local database" not in "".join(sections.values())
    assert entities.SCHEMA_PATH.resolve() == SCHEMA_FILE


def test_schema_sections_splits_given_text():
    text = (
        "-- preamble\n-- @section game\nSELECT 1;\n-- @section ledger\nSELECT 2;\n"
        "-- @section sync\nSELECT 3;\n-- @section user\nSELECT 4;\n"
    )
    sections = schema_sections(text)
    assert tuple(sections) == SECTION_NAMES
    for name, marker in zip(SECTION_NAMES, ("1", "2", "3", "4")):
        assert f"SELECT {marker};" in sections[name]
        for other in set("1234") - {marker}:
            assert f"SELECT {other};" not in sections[name]
    assert "preamble" not in "".join(sections.values())


def test_snapshot_shape():
    """game + ledger alone apply to fresh db (FK clean)"""
    sections = schema_sections()
    conn = connect(sections["game"] + "\n" + sections["ledger"])
    try:
        assert len(table_names(conn)) == 24
        assert table_names(conn) == GAME_TABLES | LEDGER_TABLES
        assert view_names(conn) == set()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_dataset_schema_version():
    assert DATASET_SCHEMA_VERSION == 1
    match = re.search(r"dataset_schema_version:\s*(\d+)", SCHEMA)
    assert match and int(match.group(1)) == DATASET_SCHEMA_VERSION


def test_works_under_untrusted_schema(tmp_path):
    path = tmp_path / "client.sqlite"
    connect(path=str(path)).close()
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA trusted_schema = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO profile (name) VALUES ('p')")
        created, updated = conn.execute("SELECT created_at, updated_at FROM profile").fetchone()
        assert TIMESTAMP.match(created) and TIMESTAMP.match(updated)
        conn.execute("SELECT * FROM gear_set_status").fetchall()
        conn.execute("SELECT * FROM stat_assignable").fetchall()
    finally:
        conn.close()


# fixtures + keys
def test_full_fixture_fk_clean(full):
    assert full.execute("PRAGMA foreign_key_check").fetchall() == []
    for table in table_names(full):
        assert full.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 1, table


def test_max_hash_id_fits(base):
    top = 2**63 - 1
    base.execute("INSERT INTO skill_effect VALUES (?, NULL, 'big')", (top,))
    assert base.execute("SELECT effect_id FROM skill_effect WHERE description = 'big'").fetchone() == (top,)


def test_defaults(base):
    row = base.execute("SELECT state, evo_depth, retired_revision FROM equipment WHERE uid = 'e1'").fetchone()
    assert row == ("live", 0, None)
    base.execute("INSERT INTO profile (name) VALUES ('p')")
    created, updated = base.execute("SELECT created_at, updated_at FROM profile").fetchone()
    assert TIMESTAMP.match(created) and TIMESTAMP.match(updated)


# CHECK constraints
EQUIPMENT_INSERT = (
    "INSERT INTO equipment (uid, name, rarity, gear_type, cost, element_id, max_level, entry_kind, "
    "keep_reason, state, first_seen, last_seen, last_changed_revision) VALUES (:k, 'n', 'ur', 'sword', 1, "
    "'fire', 1, {kind}, "
    "'r', {state}, 't', 't', 1)"
)

CHECKS = [
    (
        "K1 element_relation.kind",
        "INSERT INTO element_relation VALUES ('fire', 'water', :v)",
        "strong",
        ["effective", "weakness"],
    ),
    (
        "K2 equipment.entry_kind",
        EQUIPMENT_INSERT.format(kind=":v", state="'live'"),
        "stub",
        ["catalog", "reference"],
    ),
    (
        "K3 equipment.state",
        EQUIPMENT_INSERT.format(kind="'catalog'", state=":v"),
        "gone",
        ["live", "retired"],
    ),
    (
        "K4 effect_link.owner_kind",
        "INSERT INTO effect_link (owner_kind, owner_id, ordinal, effect_id) VALUES (:v, :k, 1, 1)",
        "item",
        sorted(OWNER_KINDS),
    ),
    (
        "K5 proc.element_position",
        "INSERT INTO proc (proc_id, family_id, raw_name, element_position) VALUES (:n, 1, 'r', :v)",
        "middle",
        ["prefix", "suffix", "infix"],
    ),
    (
        "K6 evolution_edge.evidence",
        "INSERT INTO evolution_edge (kind, from_uid, to_uid, evidence, from_name, to_name) "
        "VALUES ('reforge', :k, 'y', :v, 'a', 'b')",
        "neither",
        ["before", "after", "both"],
    ),
    (
        "K7 evolution_edge.kind",
        "INSERT INTO evolution_edge (kind, from_uid, to_uid, evidence, from_name, to_name) "
        "VALUES (:v, :k, 'y', 'both', 'a', 'b')",
        "fusion",
        list(EVO_KINDS),
    ),
    (
        "K7 evolution_chain.kind",
        "INSERT INTO evolution_chain (kind, chain_id, uid, position) VALUES (:v, 'root', :k, 0)",
        "fusion",
        list(EVO_KINDS),
    ),
    (
        "K7 evolution_material.kind",
        "INSERT INTO evolution_material (kind, from_uid, to_uid, ordinal, material_kind, ref_name) "
        "VALUES (:v, :k, 'y', 1, 'gear', 'n')",
        "fusion",
        list(EVO_KINDS),
    ),
    (
        "K8 evolution_material.material_kind",
        "INSERT INTO evolution_material (kind, from_uid, to_uid, ordinal, material_kind, ref_name) "
        "VALUES ('reforge', :k, 'y', 1, :v, 'n')",
        "currency",
        ["gear", "item"],
    ),
    (
        "K9 sync_history.kind",
        "INSERT INTO sync_history (revision, kind, applied_at, app_version, package_sha256) "
        "VALUES (:n, :v, 't', 'v', 's')",
        "delta",
        ["update", "snapshot"],
    ),
]
CHECK_IDS = [c[0] for c in CHECKS]


@pytest.mark.parametrize("label, sql, bad, good", CHECKS, ids=CHECK_IDS)
def test_check_rejects(base, label, sql, bad, good):
    with pytest.raises(sqlite3.IntegrityError):
        base.execute(sql, {"v": bad, "k": "bad", "n": 900})


@pytest.mark.parametrize("label, sql, bad, good", CHECKS, ids=CHECK_IDS)
def test_check_accepts(base, label, sql, bad, good):
    for i, value in enumerate(good):
        base.execute(sql, {"v": value, "k": f"k{i}", "n": 100 + i})


def test_icon_kind_rejects_unknown(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO icon (sha256, kind, bytes) VALUES ('s', 'sprite', 1)")


def test_equipment_element_not_null(base):
    with pytest.raises(sqlite3.IntegrityError):
        base.execute(EQUIPMENT_INSERT.replace("'fire'", "NULL").format(kind="'catalog'", state="'live'"), {"k": "z"})


def test_proc_element_position_null_allowed(base):
    base.execute("INSERT INTO proc (proc_id, family_id, raw_name) VALUES (1, 1, 'r')")
    assert base.execute("SELECT element_position FROM proc").fetchone() == (None,)


@pytest.mark.parametrize("kind", ["equipment", "item", "ability"])
def test_icon_kind_vocab_accepted(db, kind):
    db.execute("INSERT INTO icon (sha256, kind, bytes) VALUES ('s', ?, 1)", (kind,))


def test_proc_family_element_size_is_not_unique(base):
    """two procs may share (family, element, size)"""
    for n, raw in ((1, "water dragon slayer xl"), (2, "water dragon slayer xl (variant)")):
        base.execute(
            "INSERT INTO proc (proc_id, family_id, element_id, size, raw_name) VALUES (?, 1, 'water', 'xl', ?)",
            (n, raw),
        )
    assert base.execute("SELECT COUNT(*) FROM proc").fetchone() == (2,)
    plan = [r[1] for r in base.execute("PRAGMA index_list(proc)")]
    assert "ix_proc_family_element_size" in plan
    unique = {r[1]: r[2] for r in base.execute("PRAGMA index_list(proc)")}
    assert unique["ix_proc_family_element_size"] == 0


def test_evo_names_not_null(db):
    for column in ("from_name", "to_name"):
        values = {"from_name": "'a'", "to_name": "'b'"}
        values[column] = "NULL"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO evolution_edge (kind, from_uid, to_uid, evidence, from_name, to_name) "
                f"VALUES ('reforge', 'x', 'y', 'both', {values['from_name']}, {values['to_name']})"
            )


def test_evo_edge_and_mats_have_no_equipment_fk(db):
    for table in ("evolution_edge", "evolution_material", "evolution_chain"):
        targets = {r[2] for r in db.execute(f"PRAGMA foreign_key_list({table})")}
        assert "equipment" not in targets, table
    db.execute(
        "INSERT INTO evolution_edge (kind, from_uid, to_uid, evidence, from_name, to_name) "
        "VALUES ('reforge', 'nobody', 'nobody2', 'both', 'a', 'b')"
    )
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


# user tables, deferred FKs
DEFERRED = [
    pytest.param("inventory", "INSERT INTO inventory (profile_id, uid) VALUES (1, 'ghost')", id="inventory"),
    pytest.param("saved_ref", "INSERT INTO saved_ref (profile_id, uid) VALUES (1, 'ghost')", id="saved_ref"),
]


@pytest.mark.parametrize("table, insert", DEFERRED)
def test_user_fk_deferred(db, table, insert):
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    assert "DEFERRABLE INITIALLY DEFERRED" in " ".join(sql.upper().split())
    db.execute("INSERT INTO profile (profile_id, name) VALUES (1, 'p')")
    db.execute("BEGIN")
    db.execute(insert)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("COMMIT")
    assert db.in_transaction, "a failed COMMIT leaves the transaction open"
    db.execute("ROLLBACK")
    assert not db.in_transaction
    assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_user_fk_resolved_before_commit(db):
    db.execute("INSERT INTO profile (profile_id, name) VALUES (1, 'p')")
    add_elements(db)
    db.execute("BEGIN")
    db.execute("INSERT INTO inventory (profile_id, uid) VALUES (1, 'late')")
    add_equipment(db, "late")
    db.execute("COMMIT")
    assert db.execute("SELECT COUNT(*) FROM inventory").fetchone() == (1,)


def test_snapshot_replace_keeps_user_rows(full):
    user_before = {t: sorted(full.execute(f"SELECT * FROM {t}").fetchall()) for t in sorted(USER_TABLES)}
    uids = [r[0] for r in full.execute("SELECT uid FROM equipment ORDER BY uid")]
    full.execute("BEGIN")
    full.execute("DELETE FROM equipment")
    for uid in uids:
        add_equipment(full, uid)
    full.execute("COMMIT")
    user_after = {t: sorted(full.execute(f"SELECT * FROM {t}").fetchall()) for t in sorted(USER_TABLES)}
    assert user_after == user_before
    assert full.execute("PRAGMA foreign_key_check").fetchall() == []


def test_catalog_delete_never_cascades_to_users(full):
    full.execute("BEGIN")
    full.execute("DELETE FROM equipment WHERE uid = '1001'")
    with pytest.raises(sqlite3.IntegrityError):
        full.execute("COMMIT")
    assert full.in_transaction
    full.execute("ROLLBACK")
    assert full.execute("SELECT COUNT(*) FROM inventory WHERE uid = '1001'").fetchone() == (2,)
    assert full.execute("SELECT COUNT(*) FROM equipment WHERE uid = '1001'").fetchone() == (1,)


def test_defer_foreign_keys_pragma(base):
    base.execute("BEGIN")
    base.execute("PRAGMA defer_foreign_keys = ON")
    add_equipment(base, "w1")
    base.execute("INSERT INTO weapon (uid, proc_id) VALUES ('w1', 77)")
    base.execute("INSERT INTO proc (proc_id, family_id, raw_name) VALUES (77, 1, 'r')")
    base.execute("COMMIT")
    assert base.execute("PRAGMA foreign_key_check").fetchall() == []


def test_defer_foreign_keys_fails_if_never_resolved(base):
    base.execute("BEGIN")
    base.execute("PRAGMA defer_foreign_keys = ON")
    add_equipment(base, "w1")
    base.execute("INSERT INTO weapon (uid, proc_id) VALUES ('w1', 77)")
    with pytest.raises(sqlite3.IntegrityError):
        base.execute("COMMIT")
    base.execute("ROLLBACK")


# cascade
def test_game_children_cascade(full):
    full.execute("DELETE FROM profile")
    full.execute("DELETE FROM equipment WHERE uid = '1003'")
    for table in ("monster", "stat", "monster_skill", "potential_level"):
        assert full.execute(f"SELECT COUNT(*) FROM {table} WHERE uid = '1003'").fetchone() == (0,), table
    full.execute("DELETE FROM equipment WHERE uid = '1001'")
    assert full.execute("SELECT COUNT(*) FROM weapon").fetchone() == (0,)
    full.execute("DELETE FROM equipment WHERE uid = '1002'")
    assert full.execute("SELECT COUNT(*) FROM defensive_gear").fetchone() == (0,)


def test_proc_children_cascade(full):
    full.execute("BEGIN")
    full.execute("PRAGMA defer_foreign_keys = ON")
    full.execute("DELETE FROM proc")
    full.execute("DELETE FROM weapon")
    full.execute("DELETE FROM defensive_gear")
    full.execute("COMMIT")
    assert full.execute("SELECT COUNT(*) FROM proc_condition").fetchone() == (0,)
    assert full.execute("SELECT COUNT(*) FROM proc_scaling").fetchone() == (0,)


def test_profile_delete_cascades(full):
    full.execute("DELETE FROM profile WHERE profile_id = 1")
    for table in ("inventory", "saved_ref", "gear_set", "gear_set_slot"):
        assert full.execute(f"SELECT COUNT(*) FROM {table} WHERE profile_id = 1").fetchone() == (0,), table
    assert full.execute("SELECT COUNT(*) FROM profile WHERE profile_id = 2").fetchone() == (1,)
    assert full.execute("SELECT COUNT(*) FROM inventory WHERE profile_id = 2").fetchone() == (1,)


def test_inventory_delete_removes_slots(full):
    full.execute("DELETE FROM inventory WHERE inv_id = 1")
    assert full.execute("SELECT inv_id FROM gear_set_slot").fetchall() == [(2,)]


def test_gear_set_delete_removes_slots(full):
    full.execute("DELETE FROM gear_set WHERE set_id = 1")
    assert full.execute("SELECT COUNT(*) FROM gear_set_slot").fetchone() == (0,)
    assert full.execute("SELECT COUNT(*) FROM inventory").fetchone() == (3,)


CROSS_PROFILE = [
    pytest.param("INSERT INTO gear_set_slot VALUES (1, 3, 2, 3)", id="slot_profile_differs_from_set"),
    pytest.param("INSERT INTO gear_set_slot VALUES (1, 3, 1, 3)", id="inventory_of_other_profile"),
]


@pytest.mark.parametrize("insert", CROSS_PROFILE)
def test_cross_profile_slot_rejected(full, insert):
    with pytest.raises(sqlite3.IntegrityError):
        full.execute(insert)


def test_inventory_row_cannot_fill_two_slots_of_one_set(full):
    with pytest.raises(sqlite3.IntegrityError):
        full.execute("INSERT INTO gear_set_slot VALUES (1, 3, 1, 1)")


def test_inventory_row_can_fill_slots_of_two_sets(full):
    full.execute("INSERT INTO gear_set (set_id, profile_id, name) VALUES (2, 1, 'other')")
    full.execute("INSERT INTO gear_set_slot VALUES (2, 1, 1, 1)")


def test_slot_number_unique_per_set(full):
    with pytest.raises(sqlite3.IntegrityError):
        full.execute("INSERT INTO gear_set_slot VALUES (1, 1, 1, 1)")


# views
def make_set(conn, costs, cap, *, states=None, profile=1, set_id=1):
    conn.execute("INSERT OR IGNORE INTO profile (profile_id, name) VALUES (?, ?)", (profile, f"p{profile}"))
    conn.execute(
        "INSERT INTO gear_set (set_id, profile_id, name, cost_cap) VALUES (?, ?, 's', ?)",
        (set_id, profile, cap),
    )
    for i, cost in enumerate(costs, start=1):
        uid = f"s{set_id}_{i}"
        state = (states or {}).get(i, "live")
        add_equipment(conn, uid, cost=cost, state=state)
        conn.execute("INSERT INTO inventory (inv_id, profile_id, uid) VALUES (?, ?, ?)", (set_id * 100 + i, profile, uid))
        conn.execute("INSERT INTO gear_set_slot VALUES (?, ?, ?, ?)", (set_id, i, profile, set_id * 100 + i))


def status(conn, set_id=1):
    return conn.execute(
        "SELECT slot_count, total_cost, retired_count, over_cap FROM gear_set_status WHERE set_id = ?",
        (set_id,),
    ).fetchone()


def test_gear_set_status(db):
    add_elements(db)
    make_set(db, [44, 45], 80)
    assert status(db) == (2, 89, 0, 1)
    db.execute("UPDATE equipment SET cost = 30 WHERE uid = 's1_1'")
    assert status(db) == (2, 75, 0, 0)
    db.execute("UPDATE equipment SET cost = 35 WHERE uid = 's1_2'")
    db.execute("UPDATE equipment SET cost = 50 WHERE uid = 's1_1'")
    assert status(db)[1] == 85 and status(db)[3] == 1
    db.execute("UPDATE equipment SET cost = 45 WHERE uid = 's1_1'")
    assert status(db)[1] == 80 and status(db)[3] == 0


def test_gear_set_status_empty_set(db):
    db.execute("INSERT INTO profile (profile_id, name) VALUES (1, 'p')")
    db.execute("INSERT INTO gear_set (set_id, profile_id, name, cost_cap) VALUES (1, 1, 'e', 80)")
    assert status(db) == (0, 0, 0, 0)


def test_gear_set_status_null_cap(db):
    add_elements(db)
    make_set(db, [44, 45], None)
    assert status(db) == (2, 89, 0, 0)


def test_gear_set_status_retired_item(db):
    add_elements(db)
    make_set(db, [10, 20], 80, states={2: "retired"})
    assert status(db) == (2, 30, 1, 0)


def test_gear_set_status_one_row_per_set(db):
    add_elements(db)
    make_set(db, [10, 20], 80, set_id=1)
    make_set(db, [30], 20, set_id=2)
    rows = db.execute("SELECT set_id, total_cost, over_cap FROM gear_set_status ORDER BY set_id").fetchall()
    assert rows == [(1, 30, 0), (2, 30, 1)]


def test_stat_assignable_matches_python(db):
    add_elements(db)
    expected = {}
    for uid, model in CATALOG.items():
        add_equipment(db, uid)
        for stats in model.stats:
            expected[(uid, stats.label)] = stats.assignable
            for tier, value in stats.values:
                db.execute("INSERT INTO stat VALUES (?, ?, ?, ?)", (uid, stats.label, tier, value))
    rows = db.execute("SELECT uid, label, assignable FROM stat_assignable").fetchall()
    assert {(u, label): bool(a) for u, label, a in rows} == expected
    assert len(rows) == len(expected), "one row per (uid, label)"
    assert any(expected.values()) and not all(expected.values())


# entities
ENTITY_TYPES = {
    "equipment", "proc", "proc_family", "skill_effect", "passive_skill", "weapon_ability", "item",
    "icon", "element", "uid_retired", "uid_alias",
}
EDGE_TABLES = {"evolution_edge", "evolution_material"}


def coverage() -> dict[tuple[str, str | None], list[str]]:
    """(table, owner_kind) -> entity types that own it, one entry per root or child"""
    found: dict[tuple[str, str | None], list[str]] = {}
    for e in ENTITIES:
        found.setdefault((e.root_table, None), []).append(e.entity_type)
        for child in e.children:
            found.setdefault((child.table, child.owner_kind), []).append(e.entity_type)
    return found


def test_entity_types():
    assert {e.entity_type for e in ENTITIES} == ENTITY_TYPES
    assert len(ENTITIES) == len(ENTITY_TYPES)
    for name in ENTITY_TYPES:
        assert entity(name).entity_type == name
    with pytest.raises(KeyError):
        entity("no_such_entity")


def test_every_game_table_covered_once():
    found = coverage()
    covered_tables = {table for table, _ in found}
    assert covered_tables == GAME_TABLES | LEDGER_TABLES
    assert not covered_tables & (SYNC_TABLES | USER_TABLES)
    link_kinds = [kind for (table, kind) in found if table == "effect_link"]
    assert all(kind is not None for kind in link_kinds)
    assert len(link_kinds) == len(set(link_kinds)) and set(link_kinds) == OWNER_KINDS
    for (table, kind), owners in found.items():
        if table == "effect_link":
            assert len(owners) == 1, (table, kind, owners)
        elif table in EDGE_TABLES:
            assert owners == ["equipment"], (table, owners)
        else:
            assert len(owners) == 1, (table, owners)


def test_owner_lookup():
    assert owner("equipment").entity_type == "equipment"
    for table in ("weapon", "defensive_gear", "monster", "monster_skill", "potential_level", "stat",
                  "evolution_edge", "evolution_chain", "evolution_material"):
        assert owner(table).entity_type == "equipment", table
    assert owner("proc_condition").entity_type == "proc"
    assert owner("proc_scaling").entity_type == "proc"
    assert owner("element_relation").entity_type == "element"
    assert owner("uid_alias").entity_type == "uid_alias"
    assert owner("uid_retired").entity_type == "uid_retired"
    assert owner("effect_link", "proc").entity_type == "proc"
    assert owner("effect_link", "passive_skill").entity_type == "passive_skill"
    assert owner("effect_link", "weapon_ability").entity_type == "weapon_ability"
    assert owner("effect_link", "monster_skill").entity_type == "equipment"
    assert owner("effect_link", "potential_level").entity_type == "equipment"


def test_root_keys_are_primary_keys(db):
    for e in ENTITIES:
        info = db.execute(f"PRAGMA table_info({e.root_table})").fetchall()          # cid, name, type, notnull, dflt, pk
        pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        assert tuple(pk) == e.key_columns, e.entity_type
        assert len(e.key_columns) == 1
        ctype = {r[1]: r[2] for r in info}[e.key_columns[0]]
        assert e.key_type == {"TEXT": "text", "INTEGER": "int"}[ctype], e.entity_type


def test_key_types():
    assert {e.entity_type: e.key_type for e in ENTITIES} == {
        "equipment": "text", "weapon_ability": "text", "item": "text", "icon": "text",
        "element": "text", "uid_retired": "text", "uid_alias": "text",
        "proc": "int", "proc_family": "int", "skill_effect": "int", "passive_skill": "int",
    }


def test_predicates_are_valid_sql(db):
    for e in ENTITIES:
        for child in e.children:
            db.execute(
                f"SELECT * FROM {child.table} WHERE {child.predicate}",
                {"key": 0 if e.key_type == "int" else "k", "key_text": "0"},
            ).fetchall()
            assert child.owner_columns and len(child.owner_columns) <= 2
            for column in child.owner_columns:
                db.execute(f"SELECT {column} FROM {child.table}").fetchall()


def test_child_where_and_root_where_execute(full):
    """? placeholder renderings run in SQLite for child of every entity"""
    for e in ENTITIES:
        for key in keys_of(full, e):
            sql, params = entities.root_where(e, key)
            assert full.execute(f"SELECT COUNT(*) FROM {e.root_table} WHERE {sql}", params).fetchone() == (1,)
            for child in e.children:
                sql, params = entities.child_where(e, child, key)
                assert sql.count("?") == len(params), (e.entity_type, child.table)
                full.execute(f"SELECT * FROM {child.table} WHERE {sql}", params).fetchall()


def test_child_where_selects_same_rows_as_named_predicate(full):
    for e in ENTITIES:
        for key in keys_of(full, e):
            for child in e.children:
                sql, params = entities.child_where(e, child, key)
                a = full.execute(f"SELECT rowid FROM {child.table} WHERE {sql}", params).fetchall()
                b = full.execute(
                    f"SELECT rowid FROM {child.table} WHERE {child.predicate}", params_for(e, key)
                ).fetchall()
                assert sorted(a) == sorted(b), (e.entity_type, child.table, key)


def test_two_column_predicates_parenthesized_and_appendable(full):
    e = entity("equipment")
    for child in e.children:
        if child.table not in ("evolution_edge", "evolution_material"):
            continue
        assert child.predicate.startswith("(") and child.predicate.endswith(")"), child.predicate
        for key in keys_of(full, e):
            sql, params = entities.child_where(e, child, key)
            assert sql.startswith("(") and sql.endswith(")"), sql
            plain = full.execute(f"SELECT rowid FROM {child.table} WHERE {sql}", params).fetchall()
            more = full.execute(f"SELECT rowid FROM {child.table} WHERE {sql} AND 1=1", params).fetchall()
            named = full.execute(
                f"SELECT rowid FROM {child.table} WHERE {child.predicate} AND 1=1", params_for(e, key)
            ).fetchall()
            assert sorted(plain) == sorted(more) == sorted(named), (child.table, key)


def test_equipment_children_order_and_edge_predicates():
    e = entity("equipment")
    order = [(c.table, c.owner_kind) for c in e.children]
    assert order.index(("effect_link", "monster_skill")) < order.index(("monster_skill", None))
    for child in e.children:
        if child.table in EDGE_TABLES:
            text = " ".join(child.predicate.lower().split())
            assert "from_uid" in text and "to_uid" in text and " or " in text, child.predicate
        if child.table == "evolution_chain":
            assert "uid" in child.predicate


def keys_of(conn, e):
    return [r[0] for r in conn.execute(f"SELECT {e.key_columns[0]} FROM {e.root_table}")]


def params_for(e, key):
    return {"key": key, "key_text": key_to_text(e, key)}


def rowids_for(conn, e, key) -> list[tuple[str, int]]:
    p = params_for(e, key)
    out = [(e.root_table, r[0]) for r in conn.execute(
        f"SELECT rowid FROM {e.root_table} WHERE {e.key_columns[0]} = :key", p)]
    for child in e.children:
        out += [(child.table, r[0]) for r in conn.execute(
            f"SELECT rowid FROM {child.table} WHERE {child.predicate}", p)]
    return out


def test_entities_partition_rows(full):
    counts: dict[tuple[str, int], int] = {}
    for e in ENTITIES:
        for key in keys_of(full, e):
            for item in rowids_for(full, e, key):
                counts[item] = counts.get(item, 0) + 1
    shipped = {r[0] for r in full.execute("SELECT uid FROM equipment")}
    for table in sorted(GAME_TABLES | LEDGER_TABLES):
        for (rowid,) in full.execute(f"SELECT rowid FROM {table}").fetchall():
            n = counts.get((table, rowid), 0)
            if table in EDGE_TABLES:
                frm, to = full.execute(f"SELECT from_uid, to_uid FROM {table} WHERE rowid = ?", (rowid,)).fetchone()
                endpoints = len({frm, to} & shipped)
                assert 1 <= endpoints <= 2, f"fixture bug: {table} row {rowid} has no shipped endpoint"
                assert n == endpoints, (table, rowid, n, endpoints)
            else:
                assert n == 1, (table, rowid, n)
    assert not {t for (t, _) in counts} & (SYNC_TABLES | USER_TABLES)


def test_fixture_has_edges_with_one_and_two_shipped_endpoints(full):
    shipped = {r[0] for r in full.execute("SELECT uid FROM equipment")}
    sizes = {len({f, t} & shipped) for f, t in full.execute("SELECT from_uid, to_uid FROM evolution_edge")}
    assert sizes == {1, 2}


def apply_reapply(conn, e, key):
    p = params_for(e, key)

    def select(table, where):
        cur = conn.execute(f"SELECT * FROM {table} WHERE {where}", p)
        cols = [d[0] for d in cur.description]
        return cols, cur.fetchall()

    root_cols, root_rows = select(e.root_table, f"{e.key_columns[0]} = :key")
    child_rows = [(c, *select(c.table, c.predicate)) for c in e.children]

    def insert(table, cols, rows):
        marks = ", ".join(f":{c}" for c in cols)
        for row in rows:
            conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})", dict(zip(cols, row))
            )

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("PRAGMA defer_foreign_keys = ON")
    for child, _cols, _rows in child_rows:
        conn.execute(f"DELETE FROM {child.table} WHERE {child.predicate}", p)
    nonkey = [c for c in root_cols if c not in e.key_columns]
    for row in root_rows:
        cols = root_cols
        marks = ", ".join(f":{c}" for c in cols)
        action = (
            "DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in nonkey) if nonkey else "DO NOTHING"
        )
        conn.execute(
            f"INSERT INTO {e.root_table} ({', '.join(cols)}) VALUES ({marks}) "
            f"ON CONFLICT ({', '.join(e.key_columns)}) {action}",
            dict(zip(cols, row)),
        )
    for _child, cols, rows in reversed(child_rows):
        insert(_child.table, cols, rows)
    conn.execute("COMMIT")


def test_aggregate_reapply_is_identity(full):
    before = dump(full)
    for e in ENTITIES:
        for key in keys_of(full, e):
            apply_reapply(full, e, key)
            assert dump(full) == before, (e.entity_type, key)
    assert full.execute("PRAGMA foreign_key_check").fetchall() == []


def test_flags():
    eq = entity("equipment")
    assert (eq.retirable, eq.aliasable, eq.deletable) == (True, True, False)
    for name in ("proc", "proc_family", "skill_effect", "passive_skill", "weapon_ability", "item", "icon", "element"):
        e = entity(name)
        assert (e.deletable, e.retirable, e.aliasable) == (True, False, False), name
    for name in ("uid_retired", "uid_alias"):
        e = entity(name)
        assert (e.deletable, e.retirable, e.aliasable) == (False, False, False), name


def test_key_text_round_trip():
    for e in ENTITIES:
        key = 2**63 - 1 if e.key_type == "int" else "1796604"
        text = key_to_text(e, key)
        assert isinstance(text, str)
        assert key_from_text(e, text) == key
        assert type(key_from_text(e, text)) is type(key)
    assert key_to_text(entity("proc"), 4068123614585592249) == ids.id_to_text(4068123614585592249)
    assert key_to_text(entity("equipment"), "1015655") == "1015655"
    with pytest.raises(ValueError):
        key_from_text(entity("proc"), "-1")


def test_effect_link_owner_id_matches_predicates(full):
    eq = entity("equipment")
    p = params_for(eq, "1003")
    selected = []
    for child in eq.children:
        if child.table == "effect_link":
            selected += [r[0] for r in full.execute(
                f"SELECT owner_kind FROM effect_link WHERE {child.predicate}", p)]
    assert sorted(selected) == ["monster_skill", "potential_level"]
