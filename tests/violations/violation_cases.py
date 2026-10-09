from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

import dbt_harness as P
from ul_house import rows as R
from ul_house.build.load import LoadError

ERROR = ("error",)


@dataclass(frozen=True)
class Case:
    id: str
    invariant: str
    mutate_seed: Callable[[Path], None] | None = None
    mutate_records: Callable[[R.Records], R.Records] | None = None
    mutate_project: Callable[[Path], None] | None = None
    mutate_history: Callable[[Path], None] | None = None
    policy: Callable[[dict], None] | None = None
    previous: bool = False
    select: tuple[str, ...] = ()
    canary_failed: bool | None = False
    delete_run_results: bool = False
    raises: type[Exception] | None = None
    failing: tuple[str, ...] = ()
    no_failing: bool = False
    findings: tuple[tuple[str, str | None, str], ...] = ()
    sources: tuple[str, ...] = ()
    extra: Callable | None = None
    levels: tuple[str, ...] = ERROR


def seed_sql(*statements: str):
    return lambda seed: P.edit_seed(seed, *statements)


def history_sql(*statements: str):
    def mutate(path: Path):
        import duckdb
        con = duckdb.connect(str(path))
        try:
            for s in statements:
                con.execute(s)
        finally:
            con.close()
    return mutate


def csv_lines(name: str, *lines: str):
    return lambda project: P.append_csv(project, name, *lines)


def on(uid: str):
    return lambda r: r.source_uid == uid


# records mutations
def blank_name(r):
    return P.patch(r, "stg_equipment", where=on("1015655"), name="   ")


def duplicate_stat(r):
    return P.patch(r, "stg_stat", append=(next(s for s in r.stg_stat if s.source_uid == "1015655"),))


def orphan_stat(r):
    return P.patch(r, "stg_stat", append=(R.StgStat("7777777", "atk", "initial", 1, 1, 1),))


def element_none(r):
    return P.patch(r, "stg_equipment", where=on("1015655"), element_id=None)


def unknown_element(r):
    return P.patch(r, "stg_equipment", where=on("1015655"), element_id="zz")


def evolution(uid: str, kind: str, **changes):
    def mutate(r):
        return P.patch(r, "stg_evolution", where=lambda e: e.source_uid == uid and e.kind == kind, **changes)
    return mutate


def chain(*steps):
    def mutate(r):
        for step in steps:
            r = step(r)
        return r
    return mutate


def cycle(r):
    r = evolution("1015655", "reforge", before_uid="1015157", before_name="x")(r)
    return P.patch(r, "stg_evolution", append=(R.StgEvolution("1015157", "reforge", "1015655", "y", None, None),))


def fire_name_water_scaling(r):
    return P.patch(r, "stg_proc", where=on("1015655"), name="fire dragon slayer xl")


def drop_owner(table: str, **kw):
    def mutate(r):
        return P.patch(r, table, where=lambda x: x.source_uid == kw["uid"] and kw.get("ordinal") in (None, getattr(x, "ordinal", None)),
                       drop=True)
    return mutate


def drop_max_tiers(uid: str):
    return lambda r: P.patch(r, "stg_stat", where=lambda s: s.source_uid == uid and s.tier.startswith("max"), drop=True)


def bump_stat(uid: str):
    return lambda r: P.patch(r, "stg_stat", where=lambda s: s.source_uid == uid and s.label == "atk" and s.tier == "initial",
                             value=10**6)


def monster_without_potential(r):
    levels = {x.source_uid for x in r.stg_potential_level}
    max2 = {s.source_uid for s in r.stg_stat if s.tier == "max2"}
    both = sorted(levels & max2)
    if not both:
        pytest.skip("no fixture monster has both max2 tiers and potential levels")
    return P.patch(r, "stg_potential_level", where=on(both[0]), drop=True)


# proj mutations
def mart_stat_value_as_varchar(project: Path):
    import yaml
    for path in sorted((project / "models").rglob("*.yml")):
        data = yaml.safe_load(path.read_text())
        for model in (data or {}).get("models", []) or []:
            if model.get("name") == "mart_stat":
                for column in model["columns"]:
                    if column["name"] == "value":
                        column["data_type"] = "varchar"
                        path.write_text(yaml.safe_dump(data, sort_keys=False))
                        return
    raise AssertionError("mart_stat.value not found in any models/*.yml")

def break_effect_link(kind: str):
    marks = {
        "proc": ("cast(p.proc_id as varchar) as owner_id", "cast(p.proc_id as varchar) || 'x' as owner_id"),
        "weapon_ability": ("select 'weapon_ability', a.uid,", "select 'weapon_ability', a.uid || 'x',"),
        "monster_skill": ("cast(s.skill_id as varchar), l.ordinal", "cast(s.skill_id as varchar) || 'x', l.ordinal"),
        "passive_skill": ("cast(ps.passive_id as varchar), l.ordinal", "cast(ps.passive_id as varchar) || 'x', l.ordinal"),
    }

    def mutate(project: Path):
        path = project / "models" / "intermediate" / "int_effect_link.sql"
        text = path.read_text()
        old, new = marks[kind]
        assert old in text, f"int_effect_link.sql no longer contains {old!r}"
        path.write_text(text.replace(old, new))
    return mutate


def churn_policy(data: dict) -> None:
    data["check"]["unexplained_churn"]["escalate"].update(min_count=1, min_pct=10.0)


def replace_in(relpath: str, old: str, new: str):
    def mutate(project: Path):
        path = project / relpath
        text = path.read_text()
        assert old in text, f"{relpath} no longer contains {old!r}"
        path.write_text(text.replace(old, new, 1))
    return mutate


def stray_main_table(project: Path):
    (project / "models" / "reports" / "stray_table.sql").write_text(
        "{{ config(materialized='table', schema='main') }}\nselect 1 as x\n")


def scaling_line(uid: str, ordinal: int, old: str, new: str):
    def mutate(r):
        (line,) = [x for x in r.stg_proc_scaling if x.source_uid == uid and x.ordinal == ordinal]
        assert old in line.description
        return P.patch(r, "stg_proc_scaling", where=lambda x: x is line, description=line.description.replace(old, new))
    return mutate


def rename_proc(uid: str, name: str):
    return lambda r: P.patch(r, "stg_proc", where=on(uid), name=name)


def tier_rename(uid: str, old: str, new: str):
    return lambda r: P.patch(r, "stg_stat", where=lambda s: s.source_uid == uid and s.tier == old, tier=new)


def label_rename(uid: str, old: str, new: str):
    return lambda r: P.patch(r, "stg_stat", where=lambda s: s.source_uid == uid and s.label == old, label=new)


def shipped_parse_failure_checks(built):
    assert [r[0] for r in built.q("SELECT uid FROM build.shipped_parse_failures")] == ["1015157"]
    assert built.q("SELECT parser_version FROM build.shipped_parse_failures")[0][0] >= 1
    changes = built.changes()
    assert not [k for k in changes if k == ("equipment", "1015157")], changes
    assert "DELETE" not in changes.values(), changes
    assert built.q("SELECT count(*) FROM equipment WHERE uid = '1015157'") == [(0,)]


def alias_deferred_checks(built):
    assert built.q("SELECT count(*) FROM uid_alias") == [(0,)]



GARBAGE_UIDS = [f"19990{n:02d}" for n in range(3, 8)]


def garbage_new_pages(count: int):
    def mutate(seed: Path):
        for uid in GARBAGE_UIDS[:count]:
            P.clone_item(seed, "1015655", uid, icon=False)
            P.garbage_page(seed, uid)
    return mutate


def gone(*uids: str):
    return seed_sql(*[f"UPDATE frontier SET state = 'gone' WHERE item_id = '{u}'" for u in uids])


def new_item_without_max_tiers(seed: Path):
    P.clone_item(seed, "1015655", "1999010", discovered_at="2026-10-19T00:00:00Z")


def corrupt_seed_meta_fingerprint(seed: Path):
    P.edit_seed(seed, "UPDATE meta SET value = 'x' WHERE key = 'scope.fingerprint'")


CASES: tuple[Case, ...] = (
    Case("V01a-required-record-field-is-none", "C2: a required record field is never NULL",
         mutate_records=element_none, raises=LoadError),
    Case("V01b-blank-name", "equipment.name is required", mutate_records=blank_name,
         failing=(r"equipment",)),
    Case("V01c-unknown-element", "equipment.element_id -> element", mutate_records=unknown_element,
         failing=(r"(relationships|accepted_values).*element",)),
    Case("V02-duplicate-key", "staging keys are unique", mutate_records=duplicate_stat,
         failing=(r"stg_stat|mart_stat|stat",)),
    Case("V03-orphan-child", "every per-page child has an equipment", mutate_records=orphan_stat,
         failing=(r"relationships.*stat|stat.*relationships|stg_stat|mart_stat",)),
    Case("V04a-effect-link-proc-owner", "effect_link(proc) -> proc",
         mutate_project=break_effect_link("proc"), failing=(r"polymorphic_relationship.*proc",)),
    Case("V04b-effect-link-ability-owner", "effect_link(weapon_ability) -> weapon_ability",
         mutate_project=break_effect_link("weapon_ability"), failing=(r"polymorphic_relationship.*ability",)),
    Case("V04c-effect-link-skill-owner", "effect_link(monster_skill) -> monster_skill",
         mutate_project=break_effect_link("monster_skill"), failing=(r"polymorphic_relationship.*skill",)),
    Case("V04d-effect-link-passive-owner", "effect_link(passive_skill) -> passive_skill",
         mutate_project=break_effect_link("passive_skill"), failing=(r"polymorphic_relationship.*passive",)),
    Case("V05a-closed-vocabulary-rarity", "rarity is a closed vocabulary (error)",
         mutate_records=lambda r: P.patch(r, "stg_equipment", where=on("1015655"), rarity="zz"),
         failing=(r"rarity|vocabulary",)),
    Case("V05b-open-vocabulary-gear-type", "an unknown gear_type is a policy finding (P23)",
         mutate_records=lambda r: P.patch(r, "stg_equipment", where=on("1015655"), gear_type="spoon"),
         no_failing=True, findings=(("unknown_vocabulary", "catalog", "warning"),), levels=("warning",)),
    Case("V06-evolution-contradiction", "§5.6 contradiction is wiki_contradiction",
         mutate_records=evolution("4434015", "reforge", after_uid="1999002", after_name="ghost"),
         no_failing=True, findings=(("wiki_contradiction", "catalog", "warning"),), levels=("warning",)),
    Case("V07-two-predecessors", "§5.6 one predecessor is wiki_contradiction",
         mutate_records=evolution("1015655", "reforge", after_uid="1796604", after_name="x"),
         no_failing=True, findings=(("wiki_contradiction", "catalog", "warning"),), levels=("warning",)),
    Case("V08-evolution-cycle", "evolution graphs have no cycles (error)", mutate_records=cycle,
         failing=(r"evolution_no_cycle",)),
    Case("V09-proc-element-disagreement", "§5.4 name element vs scaling element is wiki_contradiction",
         mutate_records=fire_name_water_scaling, no_failing=True,
         findings=(("wiki_contradiction", "catalog", "warning"),), levels=("warning",)),
    Case("V10-unresolved-ref-is-not-an-error", "§5.6 unresolved references are reported only",
         mutate_records=evolution("1015655", "reforge", before_uid="9999999", before_name="never crawled"),
         no_failing=True, findings=(("info__evolution_unresolved_ref", None, "info"),), levels=("pass",)),
    Case("V11-equipment-never-deleted", "an equipment key of the previous release must still exist", previous=True,
         mutate_history=history_sql("INSERT INTO entity_current VALUES ('equipment', '8888888', 'h', 'c', 1, 'INSERT', "
                                    "'live', NULL, '2026-10-04T00:00:00Z')"),
         failing=(r"history__equipment_never_deleted",)),
    Case("V12-ledger-append-only", "a shipped alias never changes", previous=True,
         mutate_history=history_sql("INSERT INTO uid_alias VALUES ('1015655', '1796604', 1, 'abc')"),
         mutate_project=csv_lines("confirmed_aliases.csv", "1015655,1015157,other target"),
         failing=(r"alias_csv_disagrees_with_ledger",)),
    Case("V13-alias-cycle", "aliases never form a cycle",
         mutate_project=csv_lines("confirmed_aliases.csv", "1015655,1015157,a", "1015157,1015655,b"),
         failing=(r"alias_cycle",)),
    Case("V14-confirmed-and-rejected", "a pair is not both confirmed and rejected",
         mutate_project=lambda p: (P.append_csv(p, "confirmed_aliases.csv", "1015655,1015157,a"),
                                   P.append_csv(p, "rejected_aliases.csv", "1015655,1015157,b")),
         failing=(r"alias_confirmed_and_rejected",)),
    Case("V15-alias-target-missing", "an alias target is shipped",
         mutate_project=csv_lines("confirmed_aliases.csv", "1014667,5555555,a"),
         failing=(r"alias_target_missing",)),
    Case("V16-live-uid-never-aliased", "a live uid is never re-pointed",
         mutate_project=csv_lines("confirmed_aliases.csv", "1015655,1015157,a"),
         failing=(r"alias_old_uid_live",)),
    Case("V17-scope-fingerprint", "C1: the seed was crawled under this scope", mutate_seed=corrupt_seed_meta_fingerprint,
         failing=(r"contract__seed_scope_matches",)),
    Case("V18-frontier-contract-failure", "C1: no frontier row carries a contract failure",
         mutate_seed=seed_sql("UPDATE frontier SET last_error = 'contract: label' WHERE item_id = '1015157'"),
         failing=(r"contract__frontier_contract_failure",)),
    Case("V19-model-contract", "C3: a mart's shape is its contract", mutate_project=mart_stat_value_as_varchar,
         failing=(r"model\.ul_house\.mart_stat",)),
    Case("V20-dbt-did-not-run", "no run_results, no release", delete_run_results=True),
    Case("V21-parse-failure", "a page that does not parse is a warning", previous=True,
         mutate_seed=garbage_new_pages(1), no_failing=True,
         findings=(("parse_failure", "catalog", "warning"),), levels=("warning",)),
    Case("V22-parse-failure-escalates", "5 parse failures of 8 hold the release", previous=True,
         mutate_seed=garbage_new_pages(5), no_failing=True,
         findings=(("parse_failure", "catalog", "blocker"),), levels=("blocker",)),
    Case("V23-retired", "a retirement is a warning", previous=True, mutate_seed=gone("1500502"), no_failing=True,
         findings=(("retired", "catalog", "warning"),), levels=("warning",)),
    Case("V24-count-drop", "a quarter of the catalog vanishing holds the release", previous=True,
         mutate_seed=gone("1015157", "1500502"), no_failing=True,
         findings=(("count_drop", "catalog", "blocker"),), levels=("blocker",)),
    Case("V25-optional-field", "a catalog item without max tiers is a warning", previous=True,
         mutate_records=drop_max_tiers("1015157"), no_failing=True,
         findings=(("optional_field_null", "catalog", "warning"),), levels=("warning",)),
    Case("V26-optional-field-new-item", "the same on a new item is info", previous=True,
         mutate_seed=new_item_without_max_tiers, mutate_records=drop_max_tiers("1999010"), no_failing=True,
         findings=(("optional_field_null", "new_item", "info"),), levels=("pass",)),
    Case("V27-icon-missing", "a missing icon is a warning", previous=True,
         mutate_seed=seed_sql("DELETE FROM asset WHERE item_id = '1015157'"), no_failing=True,
         findings=(("icon_missing", "catalog", "warning"),), levels=("warning",)),
    Case("V28-unexplained-churn", "an update without a parser change escalates", previous=True,
         mutate_records=bump_stat("1015157"), policy=churn_policy, no_failing=True,
         findings=(("unexplained_churn", "catalog", "blocker"),), levels=("blocker",)),
    Case("V29-unlisted-release", "three runs unlisted is a warning",
         mutate_seed=seed_sql("UPDATE unlisted_release SET runs = 3"), no_failing=True,
         findings=(("unlisted_release", "catalog", "warning"),), levels=("warning",)),
    Case("V30-canary", "a failed canary holds the release", canary_failed=True, no_failing=True,
         findings=(("canary", "catalog", "blocker"),), levels=("blocker",)),
    Case("V31-max-potential-iff-hidden-potential", "§3.5: max2 tiers imply hidden potential (info)",
         mutate_records=monster_without_potential, no_failing=True,
         findings=(("info__max_potential_iff_hidden_potential", None, "info"),), levels=("pass",)),
    Case("V32-edge-endpoint-shipped", "every edge has a shipped endpoint (P11)",
         mutate_project=replace_in("models/marts/mart_evolution_edge.sql", "from {{ ref('int_evolution_edge') }}",
                                   "from {{ ref('int_evolution_edge') }} union all select 'reforge', '7000001', "
                                   "'7000002', 'both', 'a', 'b'"),
         failing=(r"evolution_edge_endpoint_shipped",)),
    Case("V33-one-operation-per-key", "a revision holds one operation per entity key",
         mutate_project=replace_in("models/intermediate/int_changes.sql", "  union all\n  select p.entity_type, p.entity_key, 'DELETE'",
                                   "  union all\n  select c.entity_type, c.entity_key, 'UPDATE', c.row_hash, c.content_hash, c.state, "
                                   "c.retired_revision from cur c where c.entity_type = 'equipment' and c.entity_key = '1015655'\n"
                                   "  union all\n  select p.entity_type, p.entity_key, 'DELETE'"),
         failing=(r"history__changes_one_op_per_key",)),
    Case("V34-ledger-append-only-direct", "a ledger row of the previous history is never dropped", previous=True,
         mutate_history=history_sql("INSERT INTO uid_retired VALUES ('1500502', 1, 'gone')"),
         mutate_project=replace_in("models/marts/mart_uid_retired.sql", "from {{ source('prev', 'uid_retired') }}",
                                   "from {{ source('prev', 'uid_retired') }} where false"),
         failing=(r"history__ledger_append_only",)),
    Case("V35-crawl-not-complete", "C1: the seed's last complete run is complete",
         mutate_seed=seed_sql("UPDATE crawl_run SET status = 'partial'"), failing=(r"contract__crawl_complete",)),
    Case("V36-meta-keys", "C1: the seed meta carries the keys the transform relies on",
         mutate_seed=seed_sql("DELETE FROM meta WHERE key = 'scope.fingerprint'"), failing=(r"contract__meta_keys",)),
    Case("V37-main-holds-only-shipped-tables", "main of mart.duckdb is exactly the shipped tables (P3)",
         mutate_project=stray_main_table, failing=(r"mart_main_is_exactly_shipped",)),
    Case("V38-alias-deferred", "an alias whose old uid is not retired is deferred, not recorded",
         mutate_project=csv_lines("confirmed_aliases.csv", "1014667,1015655,not retired yet"), no_failing=True,
         findings=(("info__alias_deferred", None, "info"),), levels=("pass",), extra=alias_deferred_checks),
    Case("V39-unknown-stat-label", "an unknown stat label is a policy finding (P23)",
         mutate_records=label_rename("1015655", "atk", "zzz"), no_failing=True,
         findings=(("unknown_vocabulary", "catalog", "warning"),), sources=("gate__unknown_vocabulary__stat_label",),
         levels=("warning",)),
    Case("V40-unknown-stat-tier", "an unknown stat tier is a policy finding (P23)",
         mutate_records=tier_rename("1015655", "max2", "max3"), no_failing=True,
         findings=(("unknown_vocabulary", "catalog", "warning"),), sources=("gate__unknown_vocabulary__stat_tier",),
         levels=("warning",)),
    Case("V41-unknown-scale-kind", "an unknown proc scale_kind is a policy finding (P23)",
         mutate_records=scaling_line("1015655", 1, "ability power", "mystery power"), no_failing=True,
         findings=(("unknown_vocabulary", "catalog", "warning"),), sources=("gate__unknown_vocabulary__scale_kind",),
         levels=("warning",)),
    Case("V42-unknown-proc-size", "an unknown proc size token is a policy finding (P23)",
         mutate_records=rename_proc("1015655", "water dragon slayer 4xl"), no_failing=True,
         findings=(("unknown_vocabulary", "catalog", "warning"),), sources=("gate__unknown_vocabulary__proc_size",),
         levels=("warning",)),
    Case("V43-proc-scaling-single-element", "one proc's scaling lines name one element (P23 wiki_contradiction)",
         mutate_records=scaling_line("1015655", 2, "water elemental", "fire elemental"), no_failing=True,
         findings=(("wiki_contradiction", "catalog", "warning"),),
         sources=("gate__wiki_contradiction__proc_scaling_single_element",), levels=("warning",)),
    Case("V44-shipped-uid-stops-parsing", "P29/P31: a shipped uid whose page breaks keeps its state and is carried forward",
         previous=True, mutate_seed=lambda seed: P.garbage_page(seed, "1015157"), no_failing=True,
         findings=(("parse_failure", "catalog", "warning"),), levels=("warning",), extra=shipped_parse_failure_checks),
)
