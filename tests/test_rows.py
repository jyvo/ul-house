import dataclasses
import hashlib
import json
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from catalog_fixtures import CATALOG, SSR_REFORGE_UID, catalog_params
from fetch_fixtures import UIDS, cached_path, missing
from ul_house import ids
from ul_house import rows as R
from ul_house.data.elements import ELEMENT
from ul_house.models.equipment import DefensiveGear, EquipmentRef, ItemRef, Monster, Weapon
from ul_house.models.gear_evolution import Awakening, Enlightening, GearEvoMaterial, ItemEvoMaterial, Reforge
from ul_house.models.gear_mechanism import (
    HiddenPotential,
    PassiveSkill,
    PotentialLevel,
    Proc,
    ProcActivation,
    ProcScaling,
    SkillEffect,
    MonsterSkill,
    WeaponAbility,
)
from ul_house.models.equipment import Stats
from ul_house.parse import PARSER_VERSION

import sample_catalog as sc

GOLDEN = Path(__file__).parent / "golden"
HASH_ID_FILE = GOLDEN / "hash_ids.json"
EFFECT_HASH_FILE = GOLDEN / "effect_hash.json"
FINGERPRINT_FILE = GOLDEN / "rows_fingerprint.json"

HASH = "h" * 8
RUN = "r"

SPEC = {
    "stg_equipment": "source_uid:T source_hash:T model_class:T name:T rarity:T gear_type:T cost:I element_id:T max_level:I",
    "stg_weapon": "source_uid:T infusion_count:I",
    "stg_defensive_gear": "source_uid:T infusion_count:I",
    "stg_monster": "source_uid:T restrictions:T?",
    "stg_stat": "source_uid:T label:T tier:T value:I label_ordinal:I tier_ordinal:I",
    "stg_proc": "source_uid:T name:T effect_hash:T activation_rate:T? raw_effect_text:T",
    "stg_proc_condition": "source_uid:T ordinal:I condition:T",
    "stg_proc_scaling": "source_uid:T ordinal:I description:T",
    "stg_weapon_ability": "source_uid:T uid:T name:T",
    "stg_monster_skill": "source_uid:T ordinal:I name:T",
    "stg_passive_skill": "source_uid:T name:T effect_hash:T",
    "stg_potential_level": "source_uid:T level:I target:T? description:T",
    "stg_skill_effect": "source_uid:T owner_kind:T owner_ordinal:I ordinal:I target:T? description:T",
    "stg_evolution": "source_uid:T kind:T before_uid:T? before_name:T? after_uid:T? after_name:T?",
    "stg_evolution_material": "source_uid:T kind:T ordinal:I material_kind:T ref_uid:T? ref_name:T quantity:I",
    "stg_element": "element_id:T",
    "stg_element_relation": "element_id:T kind:T ordinal:I related_id:T",
    "parse_error": "source_id:T source_hash:T parser_version:I error:T run_id:T",
}
CLASS_NAMES = {
    "stg_equipment": "StgEquipment",
    "stg_weapon": "StgWeapon",
    "stg_defensive_gear": "StgDefensiveGear",
    "stg_monster": "StgMonster",
    "stg_stat": "StgStat",
    "stg_proc": "StgProc",
    "stg_proc_condition": "StgProcCondition",
    "stg_proc_scaling": "StgProcScaling",
    "stg_weapon_ability": "StgWeaponAbility",
    "stg_monster_skill": "StgMonsterSkill",
    "stg_passive_skill": "StgPassiveSkill",
    "stg_potential_level": "StgPotentialLevel",
    "stg_skill_effect": "StgSkillEffect",
    "stg_evolution": "StgEvolution",
    "stg_evolution_material": "StgEvolutionMaterial",
    "stg_element": "StgElement",
    "stg_element_relation": "StgElementRelation",
    "parse_error": "ParseError",
}
KEYS = {
    "stg_equipment": ("source_uid",),
    "stg_weapon": ("source_uid",),
    "stg_defensive_gear": ("source_uid",),
    "stg_monster": ("source_uid",),
    "stg_stat": ("source_uid", "label", "tier"),
    "stg_proc": ("source_uid",),
    "stg_proc_condition": ("source_uid", "ordinal"),
    "stg_proc_scaling": ("source_uid", "ordinal"),
    "stg_weapon_ability": ("source_uid",),
    "stg_monster_skill": ("source_uid", "ordinal"),
    "stg_passive_skill": ("source_uid",),
    "stg_potential_level": ("source_uid", "level"),
    "stg_skill_effect": ("source_uid", "owner_kind", "owner_ordinal", "ordinal"),
    "stg_evolution": ("source_uid", "kind"),
    "stg_evolution_material": ("source_uid", "kind", "ordinal"),
    "stg_element": ("element_id",),
    "stg_element_relation": ("element_id", "kind", "ordinal"),
    "parse_error": ("source_id",),
}
STAGING = tuple(t for t in SPEC if t != "parse_error")
PAGE_TABLES = tuple(t for t in STAGING if t not in ("stg_element", "stg_element_relation"))
EXPECTED_COUNTS = {
    "stg_equipment": 7, "stg_weapon": 2, "stg_defensive_gear": 1, "stg_monster": 4, "stg_stat": 38,
    "stg_proc": 3, "stg_proc_condition": 2, "stg_proc_scaling": 4, "stg_weapon_ability": 1,
    "stg_monster_skill": 5, "stg_passive_skill": 2, "stg_potential_level": 13, "stg_skill_effect": 47,
    "stg_evolution": 8, "stg_evolution_material": 18, "stg_element": 8, "stg_element_relation": 20,
    "parse_error": 0,
}


def spec_columns(table):
    out = []
    for token in SPEC[table].split():
        name, kind = token.split(":")
        out.append((name, "TEXT" if kind[0] == "T" else "INTEGER", kind.endswith("?")))
    return tuple(out)


# helpers
def page(model, *, uid=None, source_hash=HASH):
    return R.ParsedPage(source_id=uid or model.uid, source_hash=source_hash, model=model)


def all_pages():
    return [page(CATALOG[uid]) for uid in sorted(CATALOG)]


def make(pages, **kw):
    kw.setdefault("run_id", RUN)
    kw.setdefault("parser_version", 1)
    return R.rows(pages, **kw)


def only(records, table, uid):
    return [r for r in records.tables()[table] if r.source_uid == uid]


def effects(records, uid, owner_kind, owner_ordinal=1):
    recs = [
        r for r in only(records, "stg_skill_effect", uid)
        if r.owner_kind == owner_kind and r.owner_ordinal == owner_ordinal
    ]
    return tuple(SkillEffect(target=r.target, description=r.description) for r in sorted(recs, key=lambda r: r.ordinal))


def ref(uid, name):
    return EquipmentRef(uid, name) if uid is not None else None


def rebuild(records, uid):
    (eq,) = only(records, "stg_equipment", uid)
    base = dict(
        uid=uid,
        name=eq.name,
        rarity=eq.rarity,
        gear_type=eq.gear_type,
        cost=eq.cost,
        element=ELEMENT[eq.element_id] if eq.element_id is not None else None,
        max_level=eq.max_level,
    )
    stats = {}
    for r in only(records, "stg_stat", uid):
        stats.setdefault(r.label_ordinal, (r.label, {}))[1][r.tier_ordinal] = (r.tier, r.value)
    base["stats"] = tuple(
        Stats(label=label, values=tuple(tiers[i] for i in sorted(tiers)))
        for _, (label, tiers) in sorted(stats.items())
    )

    evo = {}
    for r in only(records, "stg_evolution", uid):
        mats = sorted(
            (m for m in only(records, "stg_evolution_material", uid) if m.kind == r.kind),
            key=lambda m: m.ordinal,
        )
        gear = tuple(
            GearEvoMaterial(quantity=m.quantity, gear=EquipmentRef(m.ref_uid, m.ref_name))
            for m in mats if m.material_kind == "gear"
        )
        items = tuple(
            ItemEvoMaterial(quantity=m.quantity, item=ItemRef(m.ref_uid, m.ref_name))
            for m in mats if m.material_kind == "item"
        )
        common = dict(before=ref(r.before_uid, r.before_name), after=ref(r.after_uid, r.after_name))
        if r.kind == "reforge":
            evo["reforge"] = Reforge(material=gear or None, **common)
        elif r.kind == "awakening":
            evo["awakening"] = Awakening(gear_materials=gear or None, item_materials=items or None, **common)
        else:
            evo["enlightening"] = Enlightening(gear_materials=gear or None, item_materials=items or None, **common)
    base["reforge"] = evo.get("reforge")
    base["awakening"] = evo.get("awakening")

    def proc():
        (p,) = only(records, "stg_proc", uid)
        conds = tuple(c.condition for c in sorted(only(records, "stg_proc_condition", uid), key=lambda c: c.ordinal))
        scaling = tuple(
            ProcScaling(description=s.description)
            for s in sorted(only(records, "stg_proc_scaling", uid), key=lambda s: s.ordinal)
        )
        return Proc(
            name=p.name,
            effect=effects(records, uid, "proc"),
            activation=ProcActivation(condition=conds or None, rate=p.activation_rate)
            if conds or p.activation_rate is not None else None,
            scaling=scaling or None,
        )

    if eq.model_class == "weapon":
        (w,) = only(records, "stg_weapon", uid)
        ability = None
        wa = only(records, "stg_weapon_ability", uid)
        if wa:
            ability = WeaponAbility(name=wa[0].name, effect=effects(records, uid, "weapon_ability"), uid=wa[0].uid)
        return Weapon(**base, infusion_count=w.infusion_count, skill=proc(), weapon_ability=ability)
    if eq.model_class == "defensive_gear":
        (d,) = only(records, "stg_defensive_gear", uid)
        return DefensiveGear(**base, infusion_count=d.infusion_count, skill=proc())
    assert eq.model_class == "monster"
    (m,) = only(records, "stg_monster", uid)
    skills = tuple(
        MonsterSkill(name=s.name, effect=effects(records, uid, "monster_skill", s.ordinal))
        for s in sorted(only(records, "stg_monster_skill", uid), key=lambda s: s.ordinal)
    )
    ps = only(records, "stg_passive_skill", uid)
    passive = PassiveSkill(name=ps[0].name, effect=effects(records, uid, "passive_skill")) if ps else None
    levels = sorted(only(records, "stg_potential_level", uid), key=lambda r: r.level)
    hidden = (
        HiddenPotential(
            unlocks=tuple(
                PotentialLevel(level=r.level, effect=SkillEffect(target=r.target, description=r.description))
                for r in levels
            ),
            restrictions=m.restrictions,
        )
        if levels else None
    )
    return Monster(**base, skill=skills, passive=passive, hidden_potential=hidden, enlightening=evo.get("enlightening"))


def raw_effect_text(skill):
    lines = []
    if skill.effect:
        lines.append("[effects]")
        lines += [f"target: {e.target}. {e.description}" if e.target is not None else e.description for e in skill.effect]
    conditions = (skill.activation.condition or ()) if skill.activation else ()
    rate = skill.activation.rate if skill.activation else None
    if conditions:
        lines += ["[activation]", *conditions]
    if rate is not None:
        lines += ["[activation rate]", rate]
    if skill.scaling:
        lines += ["[scaling]", *(s.description for s in skill.scaling)]
    return "\n".join(lines)


def golden(path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


# shapes
@pytest.mark.parametrize("table", list(SPEC))
def test_record_fields_equal_staging_columns(table):
    cls = R.RECORD_TYPES[table]
    assert cls.__name__ == CLASS_NAMES[table]
    assert [f.name for f in fields(cls)] == [c[0] for c in spec_columns(table)]
    assert getattr(R, CLASS_NAMES[table]) is cls
    assert cls.__dataclass_params__.frozen
    assert "__slots__" in vars(cls)


def test_evolution_tables_follow_the_5_5_ddl():
    assert [f.name for f in fields(R.StgEvolution)] == [
        "source_uid", "kind", "before_uid", "before_name", "after_uid", "after_name",
    ]
    assert [f.name for f in fields(R.StgEvolutionMaterial)] == [
        "source_uid", "kind", "ordinal", "material_kind", "ref_uid", "ref_name", "quantity",
    ]


def test_records_shape():
    assert tuple(R.STAGING_TABLES) == STAGING and len(R.STAGING_TABLES) == 17
    assert [f.name for f in fields(R.Records)] == ["run_id", "parser_version", *STAGING, "parse_errors"]
    records = make([])
    assert tuple(records.tables()) == (*STAGING, "parse_error")
    assert set(R.RECORD_TYPES) == set(R.STAGING_KEYS) == set(SPEC)
    assert R.Records.__dataclass_params__.frozen


def test_staging_keys():
    for table, key in KEYS.items():
        assert tuple(R.STAGING_KEYS[table]) == key, table
        names = {f.name for f in fields(R.RECORD_TYPES[table])}
        assert set(key) <= names


def test_frozen():
    records = make(all_pages())
    with pytest.raises(FrozenInstanceError):
        records.run_id = "other"
    with pytest.raises(FrozenInstanceError):
        records.stg_equipment[0].name = "other"
    with pytest.raises(FrozenInstanceError):
        R.ParsedPage("1", "h", None, "e").source_id = "2"


@pytest.mark.parametrize("table", list(SPEC))
def test_columns(table):
    assert tuple(R.columns(R.RECORD_TYPES[table])) == spec_columns(table)


def test_columns_examples():
    assert ("cost", "INTEGER", False) in R.columns(R.StgEquipment)
    assert ("element_id", "TEXT", False) in R.columns(R.StgEquipment)
    assert ("parser_version", "INTEGER", False) in R.columns(R.ParseError)


def test_records_carry_run_and_parser_version():
    records = make(all_pages(), run_id="run-42", parser_version=9)
    assert (records.run_id, records.parser_version) == ("run-42", 9)


def test_parse_error_has_five_ordered_fields():
    e = R.ParseError("1", "h", 7, "boom", "r")
    assert (e.source_id, e.source_hash, e.parser_version, e.error, e.run_id) == ("1", "h", 7, "boom", "r")


def test_assignable_never_staged():
    for table, cls in R.RECORD_TYPES.items():
        names = {f.name for f in fields(cls)}
        assert not names & {"assignable", "slotted"}, table


def test_parser_version():
    assert type(PARSER_VERSION) is int and PARSER_VERSION >= 1


# round trip
@pytest.mark.parametrize("uid, expected", catalog_params())
def test_round_trip(uid, expected):
    records = make([page(expected)])
    assert records.parse_errors == []
    assert rebuild(records, uid) == expected


def test_round_trip_all_pages_and_counts():
    records = make(all_pages())
    assert records.parse_errors == []
    for uid, model in CATALOG.items():
        assert rebuild(records, uid) == model
    counts = {table: len(rows) for table, rows in records.tables().items()}
    assert counts == EXPECTED_COUNTS


def test_model_class_per_page():
    records = make(all_pages())
    by_uid = {r.source_uid: r.model_class for r in records.stg_equipment}
    assert by_uid == {
        sc.UR_WEAPON.uid: "weapon",
        sc.XENO_WEAPON.uid: "weapon",
        sc.FATEWOVEN_GEAR.uid: "defensive_gear",
        sc.MON_PASSIVE.uid: "monster",
        sc.FATEWOVEN_MON.uid: "monster",
        sc.AWAKENING_MON.uid: "monster",
        sc.ENLIGHTENING_MON.uid: "monster",
    }
    assert {r.source_hash for r in records.stg_equipment} == {HASH}


def test_round_trip_from_pages():
    absent = missing()
    if absent:
        pytest.skip(f"fixture pages not cached: {', '.join(absent)}")
    pages = []
    for uid in UIDS:
        data = cached_path(uid).read_bytes()
        pages.append(R.parse_page(uid, hashlib.sha256(data).hexdigest(), data))
    assert [p.error for p in pages] == [None] * len(pages)
    records = make(pages)
    assert records.parse_errors == []
    assert {r.source_uid for r in records.stg_equipment} == set(UIDS)
    assert SSR_REFORGE_UID in UIDS
    for uid, model in CATALOG.items():
        assert rebuild(records, uid) == model


def test_parse_page_accepts_text_and_bytes():
    absent = missing()
    if absent:
        pytest.skip(f"fixture pages not cached: {', '.join(absent)}")
    uid = sc.UR_WEAPON.uid
    raw = cached_path(uid).read_bytes()
    as_bytes = R.parse_page(uid, "h", raw)
    as_text = R.parse_page(uid, "h", raw.decode("utf-8"))
    assert as_bytes.model == as_text.model == sc.UR_WEAPON
    assert (as_bytes.source_id, as_bytes.source_hash, as_bytes.error) == (uid, "h", None)


# error handling
def test_unparseable_page():
    bad = R.parse_page("1", "h", "<html><body></body></html>")
    assert bad.model is None
    assert bad.error is not None and bad.error.startswith("UnparseablePage")
    assert (bad.source_id, bad.source_hash) == ("1", "h")
    records = make([bad], parser_version=7, run_id="r")
    for table, recs in records.tables().items():
        if table.startswith("stg_") and table not in ("stg_element", "stg_element_relation"):
            assert [r for r in recs if r.source_uid == "1"] == []
    assert records.parse_errors == [R.ParseError("1", "h", 7, bad.error, "r")]


def test_parse_page_never_raises(monkeypatch):
    assert R.parse_page("1", "h", b"\xff\xfe\x00 not html at all").model is None
    assert R.parse_page("1", "h", "").model is None


def test_exception_becomes_parse_error(monkeypatch):
    def boom(html, item_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(R, "parse_html", boom)
    result = R.parse_page("1", "h", "<html></html>")
    assert result.model is None
    assert result.error == "RuntimeError: boom"


def test_error_text_is_truncated(monkeypatch):
    def boom(html, item_id):
        raise RuntimeError("x" * 5000)

    monkeypatch.setattr(R, "parse_html", boom)
    assert len(R.parse_page("1", "h", "<html></html>").error) <= 1000


def test_explicit_error_is_reported_verbatim():
    records = make([R.ParsedPage("5", "h5", None, "SomethingBroke: why")])
    assert records.parse_errors == [R.ParseError("5", "h5", 1, "SomethingBroke: why", "r")]


def test_model_without_element_is_staged_as_none():
    base = dataclasses.replace(sc.MON_PASSIVE, element=None)
    records = make([page(base)])
    (eq,) = only(records, "stg_equipment", base.uid)
    assert eq.element_id == "none" == ELEMENT["none"].id


def test_page_is_atomic():
    """page contributes all rows or none"""
    base = sc.MON_PASSIVE
    skill = dataclasses.replace(
        base.skill[0], effect=(SkillEffect(description="bad \x1f text"), *base.skill[0].effect[1:])
    )
    broken = dataclasses.replace(base, skill=(skill,))
    good = sc.FATEWOVEN_MON
    records = make([page(broken), page(good)])
    assert len(records.parse_errors) == 1
    error = records.parse_errors[0]
    assert error.source_id == broken.uid and error.error.startswith("ControlSeparator")
    assert "stg_skill_effect.description" in error.error
    for table in PAGE_TABLES:
        assert only(records, table, broken.uid) == [], table
    solo = make([page(good)])
    for table in PAGE_TABLES:
        assert only(records, table, good.uid) == only(solo, table, good.uid), table
    assert only(records, "stg_equipment", good.uid)


def test_separator_in_proc_text_also_drops_the_page():
    """checks separators before hashing"""
    base = sc.XENO_WEAPON
    broken = dataclasses.replace(
        base, skill=dataclasses.replace(base.skill, effect=(SkillEffect(description="x\x1fy"),))
    )
    records = make([page(broken), page(sc.UR_WEAPON)])
    assert [e.source_id for e in records.parse_errors] == [broken.uid]
    assert records.parse_errors[0].error.startswith("ControlSeparator")
    assert all(only(records, t, broken.uid) == [] for t in PAGE_TABLES)
    assert only(records, "stg_equipment", sc.UR_WEAPON.uid)


def test_uid_mismatch():
    records = make([R.ParsedPage("x", "h", sc.UR_WEAPON)])
    assert len(records.parse_errors) == 1
    assert records.parse_errors[0].error.startswith("UidMismatch")
    assert records.parse_errors[0].source_id == "x"
    assert all(only(records, t, "x") == [] and only(records, t, sc.UR_WEAPON.uid) == [] for t in PAGE_TABLES)


def test_duplicate_key():
    base = sc.MON_PASSIVE
    twin = HiddenPotential(
        unlocks=(
            PotentialLevel(level=1, effect=SkillEffect(description="a")),
            PotentialLevel(level=1, effect=SkillEffect(description="b")),
        )
    )
    records = make([page(dataclasses.replace(base, hidden_potential=twin))])
    assert len(records.parse_errors) == 1
    assert records.parse_errors[0].error.startswith("DuplicateKey")
    assert all(only(records, t, base.uid) == [] for t in PAGE_TABLES)


def test_unsupported_model():
    class Other:
        uid = "1"

    records = make([R.ParsedPage("1", "h", Other())])
    assert len(records.parse_errors) == 1
    assert records.parse_errors[0].error.startswith("UnsupportedModel")


def test_caller_errors():
    a = page(sc.UR_WEAPON)
    with pytest.raises(ValueError):
        make([a, a])
    with pytest.raises(ValueError):
        make([R.ParsedPage(sc.UR_WEAPON.uid, "h", sc.UR_WEAPON, "also an error")])
    with pytest.raises(ValueError):
        make([R.ParsedPage("1", "h", None, None)])


# lineage
def test_deterministic_order():
    pages = all_pages()
    assert make(pages) == make(list(reversed(pages)))
    assert make(pages) == make(pages)
    assert [r.source_uid for r in make(list(reversed(pages))).stg_equipment] == sorted(CATALOG)


def test_rows_accepts_any_iterable():
    assert make(iter(all_pages())) == make(all_pages())


def test_lineage_placement():
    records = make(all_pages())
    for table, cls in R.RECORD_TYPES.items():
        names = {f.name for f in fields(cls)}
        if table in ("stg_equipment", "parse_error"):
            assert "source_hash" in names
        else:
            assert "source_hash" not in names, table
        assert not names & {"run_id", "parser_version"} or table == "parse_error"
    staged = {r.source_uid for r in records.stg_equipment}
    for table in PAGE_TABLES:
        assert {r.source_uid for r in records.tables()[table]} <= staged, table


def test_unique_keys_hold():
    records = make(all_pages())
    for table, recs in records.tables().items():
        keys = [tuple(getattr(r, c) for c in R.STAGING_KEYS[table]) for r in recs]
        assert len(keys) == len(set(keys)), table


def test_ordinals_one_based_contiguous():
    records = make(all_pages())

    def contiguous(values):
        return sorted(values) == list(range(1, len(values) + 1))

    for table in ("stg_proc_condition", "stg_proc_scaling", "stg_monster_skill"):
        for uid in CATALOG:
            assert contiguous([r.ordinal for r in only(records, table, uid)]), (table, uid)
    for uid in CATALOG:
        stats = only(records, "stg_stat", uid)
        labels = {r.label_ordinal for r in stats}
        assert sorted(labels) == list(range(1, len(labels) + 1)), uid
        for lo in labels:
            tiers = [r.tier_ordinal for r in stats if r.label_ordinal == lo]
            assert contiguous(tiers), (uid, lo)
        groups = {}
        for r in only(records, "stg_skill_effect", uid):
            groups.setdefault((r.owner_kind, r.owner_ordinal), []).append(r.ordinal)
        for key, ordinals in groups.items():
            assert contiguous(ordinals), (uid, key)
        skill_ordinals = {r.ordinal for r in only(records, "stg_monster_skill", uid)}
        assert {o for (kind, o) in groups if kind == "monster_skill"} == skill_ordinals
        assert {o for (kind, o) in groups if kind != "monster_skill"} <= {1}
        for kind in ("reforge", "awakening", "enlightening"):
            mats = [r.ordinal for r in only(records, "stg_evolution_material", uid) if r.kind == kind]
            assert contiguous(mats), (uid, kind)
    for el in ELEMENT:
        for kind in ("effective", "weakness"):
            ords = [r.ordinal for r in records.stg_element_relation if r.element_id == el and r.kind == kind]
            assert contiguous(ords), (el, kind)


def test_sp_evo_materials_share_one_ordinal_sequence():
    records = make(all_pages())
    mats = sorted(
        (r for r in only(records, "stg_evolution_material", sc.ENLIGHTENING_MON.uid) if r.kind == "enlightening"),
        key=lambda r: r.ordinal,
    )
    assert [m.material_kind for m in mats] == ["gear"] * 3 + ["item"] * 5
    assert [m.ordinal for m in mats] == list(range(1, 9))
    assert mats[0].ref_uid == "1104903" and mats[0].quantity == 2
    assert mats[3].ref_uid == "7007" and mats[3].quantity == 5


def test_evo_rows():
    records = make(all_pages())
    by_kind = {(r.source_uid, r.kind): r for r in records.stg_evolution}
    assert len(by_kind) == 8
    ur = by_kind[(sc.UR_WEAPON.uid, "reforge")]
    assert (ur.before_uid, ur.before_name, ur.after_uid, ur.after_name) == (
        "1014667", "sea dragon's sword", None, None,
    )
    ench = by_kind[(sc.ENLIGHTENING_MON.uid, "enlightening")]
    assert (ench.before_uid, ench.after_uid) == (None, "4435353")
    assert (sc.ENLIGHTENING_MON.uid, "reforge") in by_kind
    assert (sc.XENO_WEAPON.uid, "reforge") not in by_kind


# mapping
def test_stat_rows():
    records = make(all_pages())
    rows = sorted(only(records, "stg_stat", sc.UR_WEAPON.uid), key=lambda r: (r.label_ordinal, r.tier_ordinal))
    assert [(r.label, r.tier, r.value, r.label_ordinal, r.tier_ordinal) for r in rows] == [
        ("atk", "initial", 10111, 1, 1), ("atk", "max1", 32741, 1, 2), ("atk", "max2", 72097, 1, 3),
        ("matk", "initial", 3740, 2, 1), ("matk", "max1", 12110, 2, 2), ("matk", "max2", 26666, 2, 3),
    ]


def test_equipment_row():
    (r,) = only(make(all_pages()), "stg_equipment", sc.UR_WEAPON.uid)
    assert (r.name, r.rarity, r.gear_type, r.cost, r.element_id, r.max_level) == (
        "[surging sea] sea dragon's sword", "ur", "sword", 44, "water", 120,
    )


def test_optional_children():
    records = make(all_pages())
    assert only(records, "stg_weapon_ability", sc.XENO_WEAPON.uid) == []
    (wa,) = only(records, "stg_weapon_ability", sc.UR_WEAPON.uid)
    assert (wa.uid, wa.name) == ("2130", "valiant blade")
    assert only(records, "stg_passive_skill", sc.FATEWOVEN_MON.uid) == []
    assert only(records, "stg_potential_level", sc.AWAKENING_MON.uid) == []
    (m,) = only(records, "stg_monster", sc.FATEWOVEN_MON.uid)
    assert m.restrictions and "fodder" in m.restrictions
    (m,) = only(records, "stg_monster", sc.MON_PASSIVE.uid)
    assert m.restrictions is None
    (p,) = only(records, "stg_proc", sc.XENO_WEAPON.uid)
    assert p.activation_rate is None
    assert only(records, "stg_proc_condition", sc.XENO_WEAPON.uid) == []
    assert only(records, "stg_proc_scaling", sc.XENO_WEAPON.uid) == []
    (p,) = only(records, "stg_proc", sc.UR_WEAPON.uid)
    assert p.activation_rate == "l" and p.name == "water dragon slayer xl"


def test_skill_effect_owners():
    records = make(all_pages())
    uid = sc.FATEWOVEN_MON.uid
    by_owner = {}
    for r in only(records, "stg_skill_effect", uid):
        by_owner.setdefault((r.owner_kind, r.owner_ordinal), []).append(r)
    assert set(by_owner) == {("monster_skill", 1), ("monster_skill", 2)}
    assert len(by_owner[("monster_skill", 1)]) == 8 and len(by_owner[("monster_skill", 2)]) == 10
    ur = {(r.owner_kind, r.owner_ordinal) for r in only(records, "stg_skill_effect", sc.UR_WEAPON.uid)}
    assert ur == {("proc", 1), ("weapon_ability", 1)}
    assert {(r.owner_kind, r.owner_ordinal) for r in only(records, "stg_skill_effect", sc.AWAKENING_MON.uid)} == {
        ("monster_skill", 1), ("passive_skill", 1),
    }
    nullable = [r for r in only(records, "stg_skill_effect", sc.UR_WEAPON.uid) if r.target is None]
    assert nullable, "effects without a target keep target None"


# golden file agreement
def test_effect_hash_and_ids_agree():
    records = make(all_pages())
    hashes = {v["name"]: v for v in golden(HASH_ID_FILE)["vectors"]}
    effect_vectors = {v["name"]: v for v in golden(EFFECT_HASH_FILE)["vectors"]}
    procs = {
        "proc_water_dragon_slayer_xl": sc.UR_WEAPON.uid,
        "proc_mastery_over_dark_xl": sc.FATEWOVEN_GEAR.uid,
        "proc_patriot_sword": sc.XENO_WEAPON.uid,
    }
    for name, uid in procs.items():
        (p,) = only(records, "stg_proc", uid)
        assert p.effect_hash == effect_vectors[name]["expected"], name
        assert [p.name, p.effect_hash] == hashes[name]["fields"], name
        assert ids.proc_id(p.name, p.effect_hash) == hashes[name]["expected"], name
    passives = {
        "passive_eclipses_blessing": sc.MON_PASSIVE.uid,
        "passive_eins_schwert": sc.AWAKENING_MON.uid,
    }
    for name, uid in passives.items():
        (p,) = only(records, "stg_passive_skill", uid)
        assert p.effect_hash == effect_vectors[name]["expected"], name
        assert [p.name, p.effect_hash] == hashes[name]["fields"], name
        assert ids.passive_id(p.name, p.effect_hash) == hashes[name]["expected"], name


def test_effect_hash_covers_the_whole_proc():
    for uid in (sc.UR_WEAPON.uid, sc.FATEWOVEN_GEAR.uid, sc.XENO_WEAPON.uid):
        skill = CATALOG[uid].skill
        activation = skill.activation
        expected = ids.effect_hash(
            [(e.target, e.description) for e in skill.effect],
            (activation.condition or ()) if activation else (),
            activation.rate if activation else None,
            [s.description for s in skill.scaling or ()],
        )
        (p,) = only(make(all_pages()), "stg_proc", uid)
        assert p.effect_hash == expected


def test_skill_ids_from_rows_match_golden():
    records = make(all_pages())
    hashes = {v["name"]: v for v in golden(HASH_ID_FILE)["vectors"]}
    mon = sc.FATEWOVEN_MON.uid
    assert {r.ordinal for r in only(records, "stg_monster_skill", mon)} == {1, 2}
    assert ids.skill_id(mon, 1) == hashes["skill_1500502_1"]["expected"]
    assert ids.skill_id(mon, 2) == hashes["skill_1500502_2"]["expected"]
    assert ids.skill_id(sc.MON_PASSIVE.uid, 1) == hashes["skill_1796604_1"]["expected"]


def test_owner_id_encoding():
    records = make(all_pages())
    (p,) = only(records, "stg_proc", sc.UR_WEAPON.uid)
    assert ids.id_to_text(ids.proc_id(p.name, p.effect_hash)) == "4068123614585592249"
    (wa,) = only(records, "stg_weapon_ability", sc.UR_WEAPON.uid)
    assert wa.uid == "2130"
    skill = [r for r in only(records, "stg_monster_skill", sc.FATEWOVEN_MON.uid) if r.ordinal == 1][0]
    assert ids.id_to_text(ids.skill_id(skill.source_uid, skill.ordinal)) == "2018827732041611503"
    (ps,) = only(records, "stg_passive_skill", sc.MON_PASSIVE.uid)
    assert ids.id_to_text(ids.passive_id(ps.name, ps.effect_hash)) == "7736285657851726282"
    (lv,) = [r for r in only(records, "stg_potential_level", sc.MON_PASSIVE.uid) if r.level == 1]
    assert f"{lv.source_uid}:{lv.level}" == "1796604:1"


def test_raw_effect_text():
    records = make(all_pages())
    (xeno,) = only(records, "stg_proc", sc.XENO_WEAPON.uid)
    assert xeno.raw_effect_text == "[effects]\natk & matk of swords/axes +25%"
    (ur,) = only(records, "stg_proc", sc.UR_WEAPON.uid)
    assert ur.raw_effect_text == raw_effect_text(sc.UR_WEAPON.skill)
    text = ur.raw_effect_text
    order = [text.index(h) for h in ("[effects]", "[activation]", "[activation rate]", "[scaling]")]
    assert order == sorted(order)
    assert "target: yourself. recovers 10 cost." in text
    for uid in (sc.FATEWOVEN_GEAR.uid,):
        (p,) = only(records, "stg_proc", uid)
        assert p.raw_effect_text == raw_effect_text(CATALOG[uid].skill)


# elements
def test_elements_always_emitted():
    records = make([])
    assert len(records.stg_element) == 8 and len(records.stg_element_relation) == 20
    for table, recs in records.tables().items():
        if table not in ("stg_element", "stg_element_relation"):
            assert recs == [], table
    assert {r.element_id for r in records.stg_element} == set(ELEMENT)


def test_element_relations_follow_data():
    records = make([])
    expected = []
    for el in ELEMENT.values():
        for kind, related in (("effective", el.effective), ("weakness", el.weakness)):
            expected += [(el.id, kind, i, rel) for i, rel in enumerate(related, start=1)]
    got = [(r.element_id, r.kind, r.ordinal, r.related_id) for r in records.stg_element_relation]
    assert sorted(got) == sorted(expected)
    assert make(all_pages()).stg_element == records.stg_element


# parser version fingerprint
def fingerprint(uid):
    records = R.rows([page(CATALOG[uid], source_hash="0" * 64)], run_id="fingerprint", parser_version=PARSER_VERSION)
    payload = {
        table: [dataclasses.asdict(r) for r in recs if r.source_uid == uid]
        for table, recs in records.tables().items()
        if table.startswith("stg_") and table not in ("stg_element", "stg_element_relation")
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def test_rows_fingerprint():
    assert FINGERPRINT_FILE.is_file(), "tests/golden/rows_fingerprint.json missing"
    expected = golden(FINGERPRINT_FILE)
    assert expected["parser_version"] == PARSER_VERSION, (
        "PARSER_VERSION and tests/golden/rows_fingerprint.json disagree: bump PARSER_VERSION and "
        "regenerate the file in the same change"
    )
    for uid in sorted(CATALOG):
        assert uid in expected["pages"], f"{uid} missing from rows_fingerprint.json: add it"
        assert fingerprint(uid) == expected["pages"][uid], (
            f"rows() output for {uid} changed: bump PARSER_VERSION and regenerate "
            "tests/golden/rows_fingerprint.json in the same change"
        )
