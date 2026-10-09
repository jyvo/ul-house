"""dbt project e2e on mini seed (-m dbt)"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.dbt

duckdb = pytest.importorskip("duckdb")

import dbt_harness as P
from catalog_fixtures import CATALOG
from fetch_fixtures import UIDS, cached_path
from mini_seed import MINI_CLOCK, fake_png, mini_scope
from ul_house import ids
from ul_house import rows as R
from ul_house.build import dbt as wrapper
from ul_house.build import gate
from ul_house.database import entities
from ul_house.models.equipment import DefensiveGear, Monster, Weapon
from ul_house.parse import PARSER_VERSION, parse_html

GOLDEN = Path(__file__).parent / "golden" / "hash_ids.json"
TYPE_MAP = {"TEXT": "VARCHAR", "INTEGER": "BIGINT"}
ELEMENT_COUNT = 8


def started(rev: int) -> str:
    return f"2026-10-{4 + rev:02d}T12:00:00Z"


def norm(text):
    return None if text is None else " ".join(text.split())


class Chain:
    def __init__(self, root: Path, mini_seed: Path):
        self.root, self.mini = root, mini_seed
        self._steps: dict[str, object] = {}
        self.inputs: dict[str, Path] = {}

    def once(self, name, fn):
        if name not in self._steps:
            try:
                self._steps[name] = ("ok", fn())
            except BaseException as e:
                self._steps[name] = ("err", e)
        status, value = self._steps[name]
        if status == "err":
            raise value
        return value

    def boot(self) -> P.Built:
        def run():
            seed = P.copy_seed(self.mini, self.root / "seed1")
            return P.build(seed, self.root / "boot" / "work", revision=1, started_at=started(1))
        return self.once("boot", run)

    def boot_again(self) -> P.Built:
        def run():
            seed = P.copy_seed(self.mini, self.root / "seed1b")
            return P.build(seed, self.root / "boot2" / "work", revision=1, started_at=started(1))
        return self.once("boot2", run)

    def _after(self, name: str, prev: P.Built, seed: Path, revision: int, **kw) -> P.Built:
        hist = self.root / f"{name}_history_in.duckdb"
        shutil.copyfile(prev.history_path, hist)
        P.add_release(hist, prev)
        return P.build(seed, self.root / name / "work", revision=revision, started_at=started(revision),
                       history_from=hist, **kw)

    def noop(self) -> P.Built:
        return self.once("noop", lambda: self._after("noop", self.boot(), self.boot_seed(), 2))

    def boot_seed(self) -> Path:
        self.boot()
        return self.root / "seed1" / "seed.sqlite"

    def vanish_seed(self) -> Path:
        def run():
            seed = P.copy_seed(self.mini, self.root / "seed3")
            P.edit_seed(seed, "UPDATE frontier SET state = 'gone' WHERE item_id = '1015655'",
                        "UPDATE page SET status = 404 WHERE item_id = '1015655'")
            P.clone_item(seed, "1015655", "1999001", discovered_at="2026-10-07T00:00:00Z")
            return seed
        return self.once("vanish_seed", run)

    def vanish(self) -> P.Built:
        return self.once("vanish", lambda: self._after("vanish", self.noop(), self.vanish_seed(), 3))

    def alias(self) -> P.Built:
        def run():
            project = P.copy_project(self.root / "project4")
            P.append_csv(project, "confirmed_aliases.csv", "1015655,1999001,same sword relisted")
            return self._after("alias", self.vanish(), self.vanish_seed(), 4, project_dir=project)
        return self.once("alias", run)

    def revive_seed(self) -> Path:
        def run():
            seed = P.copy_seed(self.vanish_seed(), self.root / "seed5")
            P.edit_seed(seed, "UPDATE frontier SET state = 'done' WHERE item_id = '1015655'",
                        "UPDATE page SET status = 200 WHERE item_id = '1015655'")
            return seed
        return self.once("revive_seed", run)

    def revive(self) -> P.Built:
        return self.once("revive", lambda: self._after("revive", self.alias(), self.revive_seed(), 5))

    def shared(self) -> P.Built:
        def mutate(records):
            (old,) = [p for p in records.stg_proc if p.source_uid == "1890424"]
            return P.patch(records, "stg_proc", where=lambda r: r.source_uid == "1890424", name=old.name + " cut")
        return self.once("shared", lambda: self._after("shared", self.revive(), self.revive_seed(), 6,
                                                       mutate_records=mutate))


_CHAIN: Chain | None = None


@pytest.fixture
def chain(offline_dbt, mini_seed_path) -> Chain:
    global _CHAIN
    if _CHAIN is None:
        _CHAIN = Chain(P.scratch("dbt_chain"), mini_seed_path)
    return _CHAIN


@pytest.fixture
def boot(chain) -> P.Built:
    return chain.boot()


def table_names(con, schema="main") -> list[str]:
    return sorted(r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = ?", [schema]).fetchall())


def columns(con, table, schema="main"):
    return con.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                       "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position", [schema, table]).fetchall()


def primary_key(con, table) -> list[str]:
    rows = con.execute("SELECT constraint_column_names FROM duckdb_constraints() "
                       "WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'", [table]).fetchall()
    return sorted(rows[0][0]) if rows else []


def table_hashes(built: P.Built) -> dict[str, str]:
    out = {}
    con = built.mart()
    try:
        for t in table_names(con):
            rows = con.execute(f'SELECT * FROM main."{t}"').fetchall()
            out[t] = hashlib.sha256("\n".join(sorted(map(repr, rows))).encode()).hexdigest()
    finally:
        con.close()
    return out


def models_of(page_uids=UIDS):
    return {uid: parse_html(cached_path(uid).read_text(), uid) for uid in page_uids}


MODELS = None


def models():
    global MODELS
    if MODELS is None:
        MODELS = models_of()
    return MODELS


def expected_edges():
    """(kind, from, to) -> evidence, from the parsed claims (consistent)"""
    records = R.rows([R.parse_page(u, "h", cached_path(u).read_bytes()) for u in sorted(UIDS)],
                     run_id="x", parser_version=PARSER_VERSION)
    claims: dict[tuple, set] = {}
    for e in records.stg_evolution:
        if e.before_uid:
            claims.setdefault((e.kind, e.before_uid, e.source_uid), set()).add("before")
        if e.after_uid:
            claims.setdefault((e.kind, e.source_uid, e.after_uid), set()).add("after")
    edges = {k: ("both" if len(v) == 2 else next(iter(v))) for k, v in claims.items()}
    return records, edges


class TestProject:
    def test_manifest(self, boot):
        manifest = json.loads((boot.work / "target" / "manifest.json").read_text())
        nodes = manifest["nodes"].values()
        marts = [n for n in nodes if n["resource_type"] == "model" and n["name"].startswith("mart_")]
        assert len(marts) == 24
        for n in marts:
            assert n["config"]["contract"]["enforced"] is True, n["name"]
            assert n["alias"] == n["name"][len("mart_"):], n["name"]
            assert n["schema"] == "main", n["name"]
        history = [n for n in nodes if n["resource_type"] == "model" and n.get("database") == "history"]
        assert history
        for n in history:
            if n["config"]["materialized"] == "incremental":
                assert n["config"].get("full_refresh") is False, n["name"]
                assert n["config"]["contract"]["enforced"] is True, n["name"]

    def test_every_test_stores_failures_and_only_gate_and_info_warn(self, boot):
        manifest = json.loads((boot.work / "target" / "manifest.json").read_text())
        tests = [n for n in manifest["nodes"].values() if n["resource_type"] == "test"]
        assert tests
        for n in tests:
            assert n["config"]["store_failures"] is True, n["name"]
            warn = n["config"]["severity"].lower() == "warn"
            assert warn == (n["name"].startswith(("gate__", "info__"))), n["name"]

    def test_gate_tests_map_to_known_checks_and_every_check_has_one(self, boot):
        manifest = json.loads((boot.work / "target" / "manifest.json").read_text())
        names = [n["name"] for n in manifest["nodes"].values()
                 if n["resource_type"] == "test" and n["name"].startswith("gate__")]
        checks = {n[len("gate__"):].split("__")[0] for n in names}
        assert checks <= set(gate.KNOWN_CHECKS)
        assert checks >= set(gate.KNOWN_CHECKS) - {"canary"}

    def test_no_packages(self, boot):
        manifest = json.loads((boot.work / "target" / "manifest.json").read_text())
        assert {n["package_name"] for n in manifest["nodes"].values()} == {"ul_house"}
        assert not (P.ANALYTICS / "packages.yml").exists() and not (P.ANALYTICS / "package-lock.yml").exists()

    def test_nothing_was_written_under_analytics(self, boot):
        assert not (P.ANALYTICS / "target").exists() and not (P.ANALYTICS / "logs").exists()
        assert (boot.work / "target" / "run_results.build.json").exists()

    def test_main_holds_exactly_the_shipped_tables(self, boot):
        con = boot.mart()
        try:
            assert table_names(con) == sorted(entities.GAME_TABLES + entities.LEDGER_TABLES)
        finally:
            con.close()

    def test_columns_equal_schema_sql(self, boot):
        lite = sqlite3.connect(":memory:")
        sections = entities.schema_sections()
        lite.executescript(sections["game"] + sections["ledger"])
        con = boot.mart()
        try:
            for table in entities.GAME_TABLES + entities.LEDGER_TABLES:
                info = lite.execute(f"PRAGMA table_xinfo({table})").fetchall()
                want = [(name, TYPE_MAP[dtype.upper()], not (notnull or pk)) for _, name, dtype, notnull, _, pk, _ in info]
                got = [(n, t, nullable == "YES") for n, t, nullable in columns(con, table)]
                assert got == want, table
                assert primary_key(con, table) == sorted(n for _, n, _, _, _, pk, _ in info if pk), table
        finally:
            con.close()

    def test_history_tables_equal_history_schema_sql(self, boot):
        ddl = (P.ANALYTICS / "history_schema.sql").read_text()
        ref = duckdb.connect()
        ref.execute(ddl)
        con = boot.history()
        try:
            for table in table_names(ref):
                assert columns(con, table) == columns(ref, table), table
                assert primary_key(con, table) == primary_key(ref, table), table
        finally:
            con.close()
            ref.close()


class TestGoldenHashIds:
    def test_every_vector_through_the_dbt_macro(self, boot):
        vectors = json.loads(GOLDEN.read_text())["vectors"]

        def literal(field):
            if field is None:
                return "CAST(NULL AS VARCHAR)"
            if isinstance(field, int):
                return f"CAST({field} AS BIGINT)"
            return f"decode(unhex('{field.encode('utf-8').hex()}'))"

        selects = []
        for i, v in enumerate(vectors):
            args = ", ".join(json.dumps(literal(f)) for f in v["fields"])
            selects.append(f"SELECT {i} AS i, {{{{ hash_id({args}) }}}} AS got")
        sql = "\nUNION ALL\n".join(selects)
        compiled = wrapper.compile_inline(sql, work_dir=boot.work, vars=boot.vars())
        con = duckdb.connect()
        got = dict(con.execute(compiled).fetchall())
        assert len(got) == len(vectors) >= 20
        for i, v in enumerate(vectors):
            assert got[i] == v["expected"], v["name"]
            assert got[i] == ids.hash_id(*v["fields"]), v["name"]

    def test_ids_in_the_mart_equal_python(self, boot):
        con = boot.mart()
        try:
            for effect_id, target, description in con.execute("SELECT effect_id, target, description FROM skill_effect").fetchall():
                assert effect_id == ids.effect_id(target, description)
            for family_id, name in con.execute("SELECT family_id, name FROM proc_family").fetchall():
                assert family_id == ids.family_id(name)
            for passive_id, name, effect_hash in con.execute("SELECT passive_id, name, effect_hash FROM passive_skill").fetchall():
                assert passive_id == ids.passive_id(name, effect_hash)
            for skill_id, uid, ordinal in con.execute("SELECT skill_id, uid, ordinal FROM monster_skill").fetchall():
                assert skill_id == ids.skill_id(uid, ordinal)
            hashes = dict(con.execute("SELECT source_uid, effect_hash FROM raw.stg_proc").fetchall())
            proc_of = dict(con.execute("SELECT uid, proc_id FROM weapon WHERE proc_id IS NOT NULL UNION ALL "
                                       "SELECT uid, proc_id FROM defensive_gear WHERE proc_id IS NOT NULL").fetchall())
            names = dict(con.execute("SELECT proc_id, raw_name FROM proc").fetchall())
            assert proc_of
            for uid, proc_id in proc_of.items():
                assert proc_id == ids.proc_id(names[proc_id], hashes[uid]), uid
        finally:
            con.close()


class TestBootstrap:
    def test_build_succeeded_and_nothing_failed(self, boot):
        run = json.loads((boot.work / "transform_run.json").read_text())
        assert run["dbt_success"] is True and run["revision"] == 1 and run["bootstrap"] is True
        assert boot.failing() == []
        assert boot.decision.level in ("pass", "warning")
        assert not [f for f in boot.decision.findings if f.level == "error"]

    def test_transform_run_row(self, boot):
        (row,) = boot.q("SELECT run_id, revision, bootstrap, dbt_success, parser_version, code_commit "
                        "FROM transform_run", db="history")
        assert row == (boot.run_id, 1, True, True, PARSER_VERSION, P.CODE_COMMIT)

    def test_every_test_has_a_stored_relation(self, boot):
        results = [r for r in boot.run_results()["results"] if r["unique_id"].startswith("test.")]
        assert results and all(r.get("relation_name") for r in results)

    def test_equipment_matches_the_sample_catalog(self, boot):
        got = {r[0]: r[1:] for r in boot.q(
            "SELECT uid, name, rarity, gear_type, cost, element_id, max_level, state, entry_kind FROM equipment")}
        assert set(got) == set(UIDS)
        for uid, model in CATALOG.items():
            assert got[uid][:6] == (norm(model.name), model.rarity.upper(), model.gear_type, model.cost,
                                    model.element.id, model.max_level), uid
        assert {g[6] for g in got.values()} == {"live"}
        assert got["4434015"][7] == "catalog"

    def test_stats(self, boot):
        got = {(u, l, t): v for u, l, t, v in boot.q("SELECT uid, label, tier, value FROM stat")}
        want = {(uid, s.label, tier, value) for uid, m in models().items() for s in m.stats for tier, value in s.values}
        assert {k + (v,) for k, v in got.items()} == want
        assert "max" in {k[2] for k in got}

    def test_class_tables(self, boot):
        by_class = {"weapon": set(), "defensive_gear": set(), "monster": set()}
        for uid, m in models().items():
            by_class["weapon" if isinstance(m, Weapon) else "defensive_gear" if isinstance(m, DefensiveGear) else "monster"].add(uid)
        for table, uids in by_class.items():
            assert {r[0] for r in boot.q(f"SELECT uid FROM {table}")} == uids, table
        for uid, count, ability in boot.q("SELECT uid, infusion_count, ability_uid FROM weapon"):
            m = models()[uid]
            assert count == m.infusion_count
            assert ability == (m.weapon_ability.uid if m.weapon_ability else None)
        assert "2130" in {r[0] for r in boot.q("SELECT ability_uid FROM weapon")}

    def test_monster_skills_potential_passive(self, boot):
        skills = {(u, o): n for u, o, n in boot.q("SELECT uid, ordinal, name FROM monster_skill")}
        want = {(uid, i, norm(s.name)) for uid, m in models().items() if isinstance(m, Monster) for i, s in enumerate(m.skill, 1)}
        assert {(u, o, n) for (u, o), n in skills.items()} == want
        levels = {(u, l) for u, l in boot.q("SELECT uid, level FROM potential_level")}
        assert levels == {(uid, u.level) for uid, m in models().items() if isinstance(m, Monster) and m.hidden_potential
                          for u in m.hidden_potential.unlocks}
        passives = dict(boot.q("SELECT m.uid, p.name FROM monster m JOIN passive_skill p USING (passive_id)"))
        assert passives == {uid: norm(m.passive.name) for uid, m in models().items() if isinstance(m, Monster) and m.passive}
        restrictions = dict(boot.q("SELECT uid, restrictions FROM monster WHERE restrictions IS NOT NULL"))
        assert restrictions == {uid: norm(m.hidden_potential.restrictions) for uid, m in models().items()
                                if isinstance(m, Monster) and m.hidden_potential and m.hidden_potential.restrictions}

    def test_no_potential_level_effect_links(self, boot):
        assert boot.q("SELECT count(*) FROM effect_link WHERE owner_kind = 'potential_level'") == [(0,)]

    def test_effect_links_resolve(self, boot):
        orphans = boot.q("SELECT count(*) FROM effect_link l LEFT JOIN skill_effect e USING (effect_id) WHERE e.effect_id IS NULL")
        assert orphans == [(0,)]
        counts = dict(boot.q("SELECT owner_kind, count(*) FROM effect_link GROUP BY 1"))
        assert set(counts) == {"proc", "weapon_ability", "monster_skill", "passive_skill"}

    def test_proc_decomposition(self, boot):
        rows = {r[0]: r[1:] for r in boot.q(
            "SELECT p.raw_name, f.name, p.element_id, p.size, p.element_position FROM proc p JOIN proc_family f USING (family_id)")}
        assert len(rows) == 3
        assert rows["water dragon slayer xl"] == ("dragon slayer", "water", "xl", "prefix")
        mastery = [v for k, v in rows.items() if k.startswith("mastery over")]
        assert mastery == [("mastery over", "dark", "xl", "suffix")]
        assert rows["patriot sword: infinity"] == ("patriot sword: infinity", None, None, None)

    def test_proc_scaling(self, boot):
        assert boot.q("SELECT count(*) FROM proc_scaling") == [(4,)]
        got = boot.q("SELECT s.ordinal, s.scale_kind, s.element_id, s.gear_kind, s.cap, s.pieces_needed FROM proc_scaling s "
                     "JOIN proc p USING (proc_id) WHERE p.raw_name = 'water dragon slayer xl' ORDER BY 1")
        assert got == [(1, "ability_power", "water", "weapons", "5xl+", 5), (2, "activation_rate", "water", "weapons", "xl", 5)]
        assert boot.q("SELECT count(*) FROM proc_condition") == [(2,)]

    def test_evolution_edges(self, boot):
        _, want = expected_edges()
        got = {(k, f, t): e for k, f, t, e in boot.q("SELECT kind, from_uid, to_uid, evidence FROM evolution_edge")}
        assert got == want
        assert got[("reforge", "4434015", "4435013")] == "both"

    def test_edge_names_of_unshipped_endpoints(self, boot):
        records, _ = expected_edges()
        names = {}
        for e in records.stg_evolution:
            if e.before_uid:
                names[e.before_uid] = e.before_name
            if e.after_uid:
                names[e.after_uid] = e.after_name
        for kind, f, t, fn, tn in boot.q("SELECT kind, from_uid, to_uid, from_name, to_name FROM evolution_edge"):
            if f not in UIDS:
                assert norm(fn) == norm(names[f]), (kind, f, t)
            if t not in UIDS:
                assert norm(tn) == norm(names[t]), (kind, f, t)

    def test_unshipped_endpoints_are_not_equipment(self, boot):
        edge_uids = {u for r in boot.q("SELECT from_uid, to_uid FROM evolution_edge") for u in r}
        shipped = {r[0] for r in boot.q("SELECT uid FROM equipment")}
        unshipped = edge_uids - shipped
        assert {"1014667", "1500501", "1796603", "1890423", "1015065", "4424110"} <= unshipped
        assert not unshipped & set(UIDS)

    def test_evolution_chains(self, boot):
        _, edges = expected_edges()
        pred = {(k, t): f for (k, f, t) in edges}
        shipped = set(UIDS)
        want = {}
        for kind, uid in {(k, u) for (k, f, t) in edges for u in (f, t)}:
            if uid not in shipped:
                continue
            hops, node = 0, uid
            while (kind, node) in pred:
                node, hops = pred[(kind, node)], hops + 1
            want[(kind, uid)] = (node, hops)
        got = {(k, u): (c, p) for k, c, u, p in boot.q("SELECT kind, chain_id, uid, position FROM evolution_chain")}
        assert got == want

    def test_evolution_materials_follow_the_attribution_rule(self, boot):
        records, edges = expected_edges()
        evo = {(e.source_uid, e.kind): e for e in records.stg_evolution}
        want = set()
        for m in records.stg_evolution_material:
            e = evo[(m.source_uid, m.kind)]
            f, t = (m.source_uid, e.after_uid) if e.after_uid else (e.before_uid, m.source_uid)
            want.add((m.kind, f, t, m.ordinal, m.material_kind, m.ref_uid, norm(m.ref_name), m.quantity))
        got = set(boot.q("SELECT kind, from_uid, to_uid, ordinal, material_kind, ref_uid, ref_name, quantity FROM evolution_material"))
        assert got == want
        assert all((k, f, t) in edges for k, f, t, *_ in got)

    def test_items_are_the_item_materials(self, boot):
        records, _ = expected_edges()
        want = {(m.ref_uid, norm(m.ref_name)) for m in records.stg_evolution_material if m.material_kind == "item"}
        assert set(boot.q("SELECT uid, name FROM item")) == want and len(want) == 10

    def test_icons(self, boot):
        rows = boot.q("SELECT sha256, kind, bytes FROM icon")
        assert len(rows) == 8
        assert {r[0] for r in rows} == {ids_sha(fake_png(u)) for u in UIDS}
        assert {r[1] for r in rows} == {"equipment"} and {r[2] for r in rows} == {len(fake_png("1015655"))}
        assert dict(boot.q("SELECT uid, icon_sha FROM equipment")) == {u: ids_sha(fake_png(u)) for u in UIDS}

    def test_elements(self, boot):
        assert boot.q("SELECT count(*) FROM element") == [(ELEMENT_COUNT,)]
        assert boot.q("SELECT count(*) FROM element_relation")[0][0] == 20

    def test_first_run_change_log(self, boot):
        changes = boot.changes()
        assert set(changes.values()) == {"INSERT"}
        assert not {t for t, _ in changes} & {"uid_retired", "uid_alias"}
        con = boot.mart()
        try:
            universe = set()
            for e in entities.ENTITIES:
                if e.entity_type in ("uid_retired", "uid_alias"):
                    continue
                for (key,) in con.execute(f"SELECT {e.key_columns[0]} FROM {e.root_table}").fetchall():
                    universe.add((e.entity_type, entities.key_to_text(e, key)))
        finally:
            con.close()
        assert set(changes) == universe
        assert sum(1 for t, _ in changes if t == "element") == ELEMENT_COUNT
        assert boot.q("SELECT count(DISTINCT revision), min(revision) FROM dataset_changes", db="history") == [(1, 1)]
        assert all(len(h) == 64 for (h,) in boot.q("SELECT row_hash FROM dataset_changes", db="history"))

    def test_row_version_and_entity_current_agree(self, boot):
        assert boot.q("SELECT count(*) FROM row_version", db="history") == boot.q("SELECT count(*) FROM dataset_changes", db="history")
        assert boot.q("SELECT count(*) FROM entity_current WHERE last_revision <> 1", db="history") == [(0,)]
        assert boot.q("SELECT count(*) FROM entity_current", db="history") == boot.q("SELECT count(*) FROM dataset_changes", db="history")

    def test_equipment_bookkeeping(self, boot):
        rows = boot.q("SELECT uid, last_changed_revision, first_seen, last_seen, state, retired_revision FROM equipment")
        assert {r[1] for r in rows} == {1} and {r[2] for r in rows} == {MINI_CLOCK}
        assert {r[3] for r in rows} == {started(1)} and {r[4:] for r in rows} == {("live", None)}
        assert set(boot.q("SELECT uid, first_seen, first_revision FROM equipment_seen", db="history")) == {
            (u, MINI_CLOCK, 1) for u in UIDS}

    def test_ledgers_start_empty(self, boot):
        assert boot.q("SELECT count(*) FROM uid_retired") == [(0,)] and boot.q("SELECT count(*) FROM uid_alias") == [(0,)]

    def test_population(self, boot):
        got = {s: v for s, v in boot.q("SELECT scope, value FROM population WHERE run_id = ?", boot.run_id, db="history")}
        assert got == {"catalog": 8, "reference": 0, "new_item": 0}

    def test_source_lineage_one_open_row_per_item(self, boot):
        assert boot.q("SELECT count(*), count(dbt_valid_to) FROM source_lineage", db="history") == [(8, 0)]

    def test_observability_rows(self, boot):
        assert boot.q("SELECT count(*) FROM obs_pipeline_run", db="history") == [(1,)]
        assert boot.q("SELECT count(*) FROM obs_data_quality WHERE run_id = ?", boot.run_id, db="history")[0][0] > 0
        assert boot.q("SELECT count(*) FROM obs_unlisted_release", db="history") == [(1,)]

    def test_only_expected_info_findings(self, boot):
        non_info = [(f.check, f.level) for f in boot.decision.findings if f.level != "info"]
        assert non_info == [], non_info

    def test_dbt_unit_tests_pass(self, boot):
        result = wrapper.run_unit_tests(boot.work, vars=boot.vars())
        assert result.success, result.log_tail

    def test_two_bootstrap_builds_are_identical(self, chain, boot):
        other = chain.boot_again()
        assert table_hashes(other) == table_hashes(boot)
        assert other.changes() == boot.changes()
        assert other.q("SELECT entity_type, entity_key, operation, row_hash, changed_at FROM dataset_changes ORDER BY 1, 2", db="history") == \
            boot.q("SELECT entity_type, entity_key, operation, row_hash, changed_at FROM dataset_changes ORDER BY 1, 2", db="history")


def ids_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def equipment_rows(built: P.Built) -> dict[str, dict]:
    con = built.mart()
    try:
        cur = con.execute("SELECT * FROM equipment")
        names = [d[0] for d in cur.description]
        return {r[0]: dict(zip(names, r)) for r in cur.fetchall()}
    finally:
        con.close()


class TestNoOpRerun:
    def test_no_changes_and_identical_mart(self, chain, boot):
        step = chain.noop()
        assert step.failing() == []
        assert step.changes() == {}
        assert table_hashes(step) == table_hashes(boot)

    def test_history_state_is_unchanged(self, chain, boot):
        step = chain.noop()
        for table in ("entity_current", "uid_retired", "uid_alias", "equipment_seen", "alias_candidate", "alias_rejected"):
            assert sorted(map(repr, step.q(f"SELECT * FROM {table}", db="history"))) == \
                sorted(map(repr, boot.q(f"SELECT * FROM {table}", db="history"))), table
        assert step.q("SELECT count(*), count(dbt_valid_to) FROM source_lineage", db="history") == [(8, 0)]

    def test_history_only_grows_by_the_new_run(self, chain, boot):
        step = chain.noop()
        assert step.q("SELECT count(*) FROM obs_pipeline_run", db="history") == [(2,)]
        assert step.q("SELECT count(*) FROM transform_run", db="history") == [(2,)]
        assert step.q("SELECT count(*) FROM dataset_changes WHERE revision = 1", db="history") == \
            boot.q("SELECT count(*) FROM dataset_changes", db="history")

    def test_previous_history_is_never_modified(self, chain, boot):
        step = chain.noop()
        assert P.sha256_file(step.previous_path) == step.history_in_sha

    def test_full_refresh_keeps_history(self, chain, boot):
        step = chain.noop()
        before = {t: sorted(map(repr, step.q(f"SELECT * FROM {t}", db="history")))
                  for t in ("dataset_changes", "row_version", "equipment_seen")}
        assert before["dataset_changes"], "rev 1 rows were carried in"
        result = wrapper.invoke(["build", "--full-refresh", "--exclude-resource-type", "unit_test",
                                 "--select", "ledger_dataset_changes", "ledger_row_version", "ledger_equipment_seen",
                                 "--vars", json.dumps(step.vars())], work_dir=step.work)
        assert result.success, result.log_tail
        for t, rows in before.items():
            assert sorted(map(repr, step.q(f"SELECT * FROM {t}", db="history"))) == rows, t

    def test_rerun_in_the_same_work_dir_is_idempotent(self, chain, boot):
        step = chain.noop()
        tables = ("dataset_changes", "row_version", "entity_current", "uid_retired", "equipment_seen", "population")
        before = {t: sorted(map(repr, step.q(f"SELECT * FROM {t}", db="history"))) for t in tables}
        wrapper.run_dbt(step.work, revision=2, scope=mini_scope(), bootstrap=False, code_commit=P.CODE_COMMIT)
        assert {t: sorted(map(repr, step.q(f"SELECT * FROM {t}", db="history"))) for t in tables} == before
        assert P.sha256_file(step.previous_path) == step.history_in_sha


class TestVanish:
    def test_change_log(self, chain):
        step = chain.vanish()
        assert step.failing() == []
        sha = P.sha256_file
        icon = ids_sha(fake_png("1999001"))
        assert step.changes() == {
            ("equipment", "1015655"): "RETIRE",
            ("uid_retired", "1015655"): "INSERT",
            ("equipment", "1999001"): "INSERT",
            ("icon", icon): "INSERT",
        }

    def test_retire_changes_exactly_the_p12_columns(self, chain):
        before, after = equipment_rows(chain.noop())["1015655"], equipment_rows(chain.vanish())["1015655"]
        changed = {k for k in before if before[k] != after[k]}
        assert changed == {"state", "retired_revision", "last_changed_revision"}
        assert (after["state"], after["retired_revision"], after["last_changed_revision"]) == ("retired", 3, 3)

    def test_the_retired_uid_is_still_shipped_with_its_children(self, chain):
        step = chain.vanish()
        assert step.q("SELECT count(*) FROM stat WHERE uid = '1015655'")[0][0] > 0
        assert step.q("SELECT count(*) FROM weapon WHERE uid = '1015655'") == [(1,)]

    def test_ledger_row(self, chain):
        assert chain.vanish().q("SELECT uid, since_revision, reason FROM uid_retired") == [("1015655", 3, "gone")]

    def test_new_item(self, chain):
        row = equipment_rows(chain.vanish())["1999001"]
        assert (row["state"], row["entry_kind"], row["last_changed_revision"], row["first_seen"]) == (
            "live", "catalog", 3, "2026-10-07T00:00:00Z")
        assert row["last_seen"] == started(3)

    def test_alias_candidate(self, chain):
        rows = chain.vanish().q("SELECT old_uid, new_uid, first_seen_revision, score FROM alias_candidate", db="history")
        assert rows == [("1015655", "1999001", 3, 1.0)]

    def test_gate_sees_one_retirement(self, chain):
        d = chain.vanish().decision
        f = [f for f in d.findings if f.check == "retired"]
        assert [(x.scope, x.level, x.count, x.escalated) for x in f] == [("catalog", "warning", 1, False)]
        assert d.previous_revision == 2 and d.level == "warning"

    def test_previous_history_is_never_modified(self, chain):
        step = chain.vanish()
        assert P.sha256_file(step.previous_path) == step.history_in_sha


class TestAliasConfirmed:
    def test_change_log(self, chain):
        step = chain.alias()
        assert step.failing() == []
        assert step.changes() == {("equipment", "1015655"): "ALIAS", ("uid_alias", "1015655"): "INSERT"}

    def test_ledger_row(self, chain):
        assert chain.alias().q("SELECT old_uid, new_uid, since_revision, confirmed_commit FROM uid_alias") == [
            ("1015655", "1999001", 4, P.CODE_COMMIT)]

    def test_the_old_uids_game_rows_do_not_change(self, chain):
        assert equipment_rows(chain.alias()) == equipment_rows(chain.vanish())
        assert equipment_rows(chain.alias())["1015655"]["last_changed_revision"] == 3

    def test_candidate_is_not_proposed_again(self, chain):
        assert chain.alias().q("SELECT count(*) FROM alias_candidate", db="history") == [(1,)]

    def test_everything_else_in_the_mart_is_identical(self, chain):
        a, b = table_hashes(chain.vanish()), table_hashes(chain.alias())
        assert {t for t in a if a[t] != b[t]} == {"uid_alias"}


class TestRevive:
    def test_change_log(self, chain):
        step = chain.revive()
        assert step.failing() == []
        assert step.changes() == {("equipment", "1015655"): "UPDATE"}

    def test_equipment_row(self, chain):
        row = equipment_rows(chain.revive())["1015655"]
        assert (row["state"], row["retired_revision"], row["last_changed_revision"]) == ("live", None, 5)
        assert row["last_seen"] == started(5)

    def test_ledger_only_grows(self, chain):
        step = chain.revive()
        assert step.q("SELECT uid, since_revision FROM uid_retired") == [("1015655", 3)]
        assert step.q("SELECT old_uid, new_uid FROM uid_alias") == [("1015655", "1999001")]

    def test_no_error_for_the_aliased_but_live_uid(self, chain):
        assert not [f for f in chain.revive().decision.findings if f.level == "error"]


class TestSharedDelete:
    def test_change_log(self, chain):
        step, before = chain.shared(), chain.revive()
        assert step.failing() == []
        old = before.q("SELECT proc_id FROM defensive_gear WHERE uid = '1890424'")[0][0]
        new = step.q("SELECT proc_id FROM defensive_gear WHERE uid = '1890424'")[0][0]
        assert old != new
        changes = step.changes()
        assert changes[("proc", ids.id_to_text(new))] == "INSERT"
        assert changes[("proc", ids.id_to_text(old))] == "DELETE"
        assert changes[("equipment", "1890424")] == "UPDATE"
        assert {op for op in changes.values()} <= {"INSERT", "UPDATE", "DELETE"}
        assert ("equipment", "1015655") not in changes

    def test_deleted_rows_are_gone_from_the_mart(self, chain):
        old = chain.revive().q("SELECT proc_id FROM defensive_gear WHERE uid = '1890424'")[0][0]
        step = chain.shared()
        assert step.q("SELECT count(*) FROM proc WHERE proc_id = ?", old) == [(0,)]
        assert step.q("SELECT count(*) FROM effect_link WHERE owner_kind = 'proc' AND owner_id = ?", ids.id_to_text(old)) == [(0,)]

    def test_entity_current_records_the_delete(self, chain):
        old = chain.revive().q("SELECT proc_id FROM defensive_gear WHERE uid = '1890424'")[0][0]
        rows = chain.shared().q("SELECT last_operation, last_revision FROM entity_current WHERE entity_type = 'proc' AND entity_key = ?",
                                ids.id_to_text(old), db="history")
        assert rows == [("DELETE", 6)]

    def test_equipment_is_never_deleted(self, chain):
        step = chain.shared()
        assert not [k for k, op in step.changes().items() if k[0] == "equipment" and op == "DELETE"]
        assert step.q("SELECT count(*) FROM equipment") == [(9,)]
