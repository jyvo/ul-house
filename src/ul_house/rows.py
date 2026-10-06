"""parsed models into staging records"""
import dataclasses
import types
import typing
from collections.abc import Iterable
from dataclasses import dataclass

from ul_house import ids
from ul_house.data.elements import ELEMENT
from ul_house.models.equipment import DefensiveGear, Equipment, Monster, Weapon
from ul_house.models.gear_evolution import GearEvolution, Reforge
from ul_house.models.gear_mechanism import Proc, SkillEffect
from ul_house.parse import parse_html

_CONTROL = (ids.SEPARATOR, ids.RECORD_SEPARATOR)
_ERROR_LIMIT = 1000
_MODEL_CLASS: dict[type, str] = {
    Weapon: "weapon",
    DefensiveGear: "defensive_gear",
    Monster: "monster",
}


@dataclass(frozen=True, slots=True)
class ParsedPage:
    source_id: str                      # seed page.item_id = the equipment uid
    source_hash: str                    # seed page.html_sha256
    model: Weapon | DefensiveGear | Monster | None
    error: str | None = None            # set iff model is None


@dataclass(frozen=True, slots=True)
class StgEquipment:
    source_uid: str
    source_hash: str
    model_class: str                    # weapon | defensive_gear | monster
    name: str
    rarity: str
    gear_type: str
    cost: int
    element_id: str                     # ELEMENT["none"].id when the model has no element
    max_level: int


@dataclass(frozen=True, slots=True)
class StgWeapon:
    source_uid: str
    infusion_count: int


@dataclass(frozen=True, slots=True)
class StgDefensiveGear:
    source_uid: str
    infusion_count: int


@dataclass(frozen=True, slots=True)
class StgMonster:
    source_uid: str
    restrictions: str | None


@dataclass(frozen=True, slots=True)
class StgStat:
    source_uid: str
    label: str
    tier: str
    value: int
    label_ordinal: int
    tier_ordinal: int


@dataclass(frozen=True, slots=True)
class StgProc:
    source_uid: str
    name: str
    effect_hash: str
    activation_rate: str | None
    raw_effect_text: str


@dataclass(frozen=True, slots=True)
class StgProcCondition:
    source_uid: str
    ordinal: int
    condition: str


@dataclass(frozen=True, slots=True)
class StgProcScaling:
    source_uid: str
    ordinal: int
    description: str


@dataclass(frozen=True, slots=True)
class StgWeaponAbility:
    source_uid: str
    uid: str
    name: str


@dataclass(frozen=True, slots=True)
class StgMonsterSkill:
    source_uid: str
    ordinal: int
    name: str


@dataclass(frozen=True, slots=True)
class StgPassiveSkill:
    source_uid: str
    name: str
    effect_hash: str


@dataclass(frozen=True, slots=True)
class StgPotentialLevel:
    source_uid: str
    level: int
    target: str | None
    description: str


@dataclass(frozen=True, slots=True)
class StgSkillEffect:
    source_uid: str
    owner_kind: str                     # proc | weapon_ability | monster_skill | passive_skill
    owner_ordinal: int                  # the monster skill's ordinal (else 1)
    ordinal: int
    target: str | None
    description: str


@dataclass(frozen=True, slots=True)
class StgEvolution:
    source_uid: str
    kind: str                           # reforge | awakening | enlightening
    before_uid: str | None
    before_name: str | None
    after_uid: str | None
    after_name: str | None


@dataclass(frozen=True, slots=True)
class StgEvolutionMaterial:
    source_uid: str
    kind: str
    ordinal: int
    material_kind: str                  # gear | item
    ref_uid: str | None
    ref_name: str
    quantity: int


@dataclass(frozen=True, slots=True)
class StgElement:
    element_id: str


@dataclass(frozen=True, slots=True)
class StgElementRelation:
    element_id: str
    kind: str                           # effective | weakness
    ordinal: int
    related_id: str


@dataclass(frozen=True, slots=True)
class ParseError:
    source_id: str
    source_hash: str
    parser_version: int
    error: str
    run_id: str


STAGING_TABLES: tuple[str, ...] = (
    "stg_equipment",
    "stg_weapon",
    "stg_defensive_gear",
    "stg_monster",
    "stg_stat",
    "stg_proc",
    "stg_proc_condition",
    "stg_proc_scaling",
    "stg_weapon_ability",
    "stg_monster_skill",
    "stg_passive_skill",
    "stg_potential_level",
    "stg_skill_effect",
    "stg_evolution",
    "stg_evolution_material",
    "stg_element",
    "stg_element_relation",
)

RECORD_TYPES: dict[str, type] = {
    "stg_equipment": StgEquipment,
    "stg_weapon": StgWeapon,
    "stg_defensive_gear": StgDefensiveGear,
    "stg_monster": StgMonster,
    "stg_stat": StgStat,
    "stg_proc": StgProc,
    "stg_proc_condition": StgProcCondition,
    "stg_proc_scaling": StgProcScaling,
    "stg_weapon_ability": StgWeaponAbility,
    "stg_monster_skill": StgMonsterSkill,
    "stg_passive_skill": StgPassiveSkill,
    "stg_potential_level": StgPotentialLevel,
    "stg_skill_effect": StgSkillEffect,
    "stg_evolution": StgEvolution,
    "stg_evolution_material": StgEvolutionMaterial,
    "stg_element": StgElement,
    "stg_element_relation": StgElementRelation,
    "parse_error": ParseError,
}

STAGING_KEYS: dict[str, tuple[str, ...]] = {
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


@dataclass(frozen=True, slots=True)
class Records:
    run_id: str
    parser_version: int
    stg_equipment: list[StgEquipment]
    stg_weapon: list[StgWeapon]
    stg_defensive_gear: list[StgDefensiveGear]
    stg_monster: list[StgMonster]
    stg_stat: list[StgStat]
    stg_proc: list[StgProc]
    stg_proc_condition: list[StgProcCondition]
    stg_proc_scaling: list[StgProcScaling]
    stg_weapon_ability: list[StgWeaponAbility]
    stg_monster_skill: list[StgMonsterSkill]
    stg_passive_skill: list[StgPassiveSkill]
    stg_potential_level: list[StgPotentialLevel]
    stg_skill_effect: list[StgSkillEffect]
    stg_evolution: list[StgEvolution]
    stg_evolution_material: list[StgEvolutionMaterial]
    stg_element: list[StgElement]
    stg_element_relation: list[StgElementRelation]
    parse_errors: list[ParseError]

    def tables(self) -> dict[str, list]:
        """{table: records} in STAGING_TABLES order"""
        out: dict[str, list] = {name: getattr(self, name) for name in STAGING_TABLES}
        out["parse_error"] = self.parse_errors
        return out


def columns(record_type: type) -> tuple[tuple[str, str, bool], ...]:
    """(column, 'TEXT' | 'INTEGER', nullable) per field"""
    hints = typing.get_type_hints(record_type)
    out = []
    for field in dataclasses.fields(record_type):
        hint = hints[field.name]
        nullable = False
        if isinstance(hint, types.UnionType) or typing.get_origin(hint) is typing.Union:
            args = [arg for arg in typing.get_args(hint) if arg is not type(None)]
            nullable = len(args) < len(typing.get_args(hint))
            if len(args) != 1:
                raise TypeError(f"{record_type.__name__}.{field.name}: unsupported type {hint}")
            hint = args[0]
        if hint is str:
            sql_type = "TEXT"
        elif hint is int:
            sql_type = "INTEGER"
        else:
            raise TypeError(f"{record_type.__name__}.{field.name}: unsupported type {hint}")
        out.append((field.name, sql_type, nullable))
    return tuple(out)


def parse_page(source_id: str, source_hash: str, html: str | bytes) -> ParsedPage:
    """parse.parse_html(html, source_id)"""
    try:
        model = parse_html(html, source_id)
    except Exception as e:  # noqa: BLE001 -- every failure is a parse_error row
        return ParsedPage(source_id, source_hash, None, f"{type(e).__name__}: {e}"[:_ERROR_LIMIT])
    return ParsedPage(source_id, source_hash, model)


class _PageRejected(Exception):
    """a page whose records must not be staged (the message is the parse_error text)"""


def _effect_hash(*args) -> str:
    # a separator in an input is reported as ControlSeparator by validation: every effect_hash
    # input is also a TEXT column of the same page's records
    try:
        return ids.effect_hash(*args)
    except ValueError:
        return ""


def _raw_effect_text(effects: tuple[SkillEffect, ...], conditions: tuple[str, ...], rate: str | None, scaling: tuple[str, ...]) -> str:
    """text rendering"""
    lines: list[str] = []
    if effects:
        lines += ["[effects]", *(
            f"target: {e.target}. {e.description}" if e.target is not None else e.description
            for e in effects
        )]
    if conditions:
        lines += ["[activation]", *conditions]
    if rate is not None:
        lines += ["[activation rate]", rate]
    if scaling:
        lines += ["[scaling]", *scaling]
    return "\n".join(lines)


def _effects(uid: str, owner_kind: str, owner_ordinal: int,
             effects: Iterable[SkillEffect]) -> list[StgSkillEffect]:
    return [
        StgSkillEffect(uid, owner_kind, owner_ordinal, ordinal, effect.target, effect.description)
        for ordinal, effect in enumerate(effects, 1)
    ]


def _proc(uid: str, skill: Proc | None, buffer: dict[str, list]) -> None:
    if skill is None:
        return
    
    rate = skill.activation.rate if skill.activation else None
    conditions = tuple(skill.activation.condition or ()) if skill.activation else ()
    scaling = tuple(s.description for s in skill.scaling or ())
    effects = tuple(skill.effect)
    buffer["stg_proc"].append(StgProc(
        source_uid=uid,
        name=skill.name,
        effect_hash=_effect_hash([(e.target, e.description) for e in effects], conditions, rate, scaling),
        activation_rate=rate,
        raw_effect_text=_raw_effect_text(effects, conditions, rate, scaling),
    ))
    buffer["stg_proc_condition"] += [
        StgProcCondition(uid, ordinal, condition) for ordinal, condition in enumerate(conditions, 1)
    ]
    buffer["stg_proc_scaling"] += [
        StgProcScaling(uid, ordinal, description) for ordinal, description in enumerate(scaling, 1)
    ]
    buffer["stg_skill_effect"] += _effects(uid, "proc", 1, effects)


def _evolution(uid: str, kind: str, evolution: GearEvolution | None, buffer: dict[str, list]) -> None:
    if evolution is None:
        return
    before, after = evolution.before, evolution.after
    buffer["stg_evolution"].append(StgEvolution(
        source_uid=uid,
        kind=kind,
        before_uid=before.uid if before is not None else None,
        before_name=before.name if before is not None else None,
        after_uid=after.uid if after is not None else None,
        after_name=after.name if after is not None else None,
    ))
    if isinstance(evolution, Reforge):
        materials = [("gear", m.gear, m.quantity) for m in evolution.material or ()]
    else:
        materials = [("gear", m.gear, m.quantity) for m in evolution.gear_materials or ()]
        materials += [("item", m.item, m.quantity) for m in evolution.item_materials or ()]
    buffer["stg_evolution_material"] += [
        StgEvolutionMaterial(uid, kind, ordinal, material_kind, ref.uid, ref.name, quantity)
        for ordinal, (material_kind, ref, quantity) in enumerate(materials, 1)
    ]


def _page_records(page: ParsedPage) -> dict[str, list]:
    """every record of one page; _PageRejected if the page must not be staged"""
    model = page.model
    uid = page.source_id
    model_class = _MODEL_CLASS.get(type(model))
    if model_class is None:
        raise _PageRejected(f"UnsupportedModel: {type(model).__name__}")
    if model.uid != uid:
        raise _PageRejected(f"UidMismatch: {model.uid} != {uid}")

    buffer: dict[str, list] = {name: [] for name in STAGING_TABLES}
    try:
        _build(page, model, model_class, buffer)
    except Exception as e:
        raise _PageRejected(f"{type(e).__name__}: {e}") from e

    for table, records in buffer.items():
        for record in records:
            for field in dataclasses.fields(record):
                value = getattr(record, field.name)
                if isinstance(value, str) and any(sep in value for sep in _CONTROL):
                    raise _PageRejected(f"ControlSeparator: {table}.{field.name}")

    for table, records in buffer.items():
        key_columns = STAGING_KEYS[table]
        seen: set[tuple] = set()
        for record in records:
            key = tuple(getattr(record, column) for column in key_columns)
            if key in seen:
                raise _PageRejected(f"DuplicateKey: {table} {key}")
            seen.add(key)
    return buffer


def _build(page: ParsedPage, model: Equipment, model_class: str, buffer: dict[str, list]) -> None:
    uid = page.source_id
    buffer["stg_equipment"].append(StgEquipment(
        source_uid=uid,
        source_hash=page.source_hash,
        model_class=model_class,
        name=model.name,
        rarity=model.rarity,
        gear_type=model.gear_type,
        cost=model.cost,
        element_id=(model.element or ELEMENT["none"]).id,
        max_level=model.max_level,
    ))

    for label_ordinal, stats in enumerate(model.stats, 1):
        for tier_ordinal, (tier, value) in enumerate(stats.values, 1):
            buffer["stg_stat"].append(StgStat(uid, stats.label, tier, value, label_ordinal, tier_ordinal))

    if isinstance(model, Weapon):
        buffer["stg_weapon"].append(StgWeapon(uid, model.infusion_count))
        _proc(uid, model.skill, buffer)
        if (ability := model.weapon_ability) is not None:
            buffer["stg_weapon_ability"].append(StgWeaponAbility(uid, ability.uid, ability.name))
            buffer["stg_skill_effect"] += _effects(uid, "weapon_ability", 1, ability.effect)
    elif isinstance(model, DefensiveGear):
        buffer["stg_defensive_gear"].append(StgDefensiveGear(uid, model.infusion_count))
        _proc(uid, model.skill, buffer)
    else:
        potential = model.hidden_potential
        buffer["stg_monster"].append(StgMonster(uid, potential.restrictions if potential else None))
        for ordinal, skill in enumerate(model.skill or (), 1):
            buffer["stg_monster_skill"].append(StgMonsterSkill(uid, ordinal, skill.name))
            buffer["stg_skill_effect"] += _effects(uid, "monster_skill", ordinal, skill.effect)
        if (passive := model.passive) is not None:
            buffer["stg_passive_skill"].append(StgPassiveSkill(
                uid, passive.name, _effect_hash([(e.target, e.description) for e in passive.effect]),
            ))
            buffer["stg_skill_effect"] += _effects(uid, "passive_skill", 1, passive.effect)
        for unlock in potential.unlocks if potential else ():
            buffer["stg_potential_level"].append(StgPotentialLevel(
                uid, unlock.level, unlock.effect.target, unlock.effect.description,
            ))

    _evolution(uid, "reforge", model.reforge, buffer)
    _evolution(uid, "awakening", model.awakening, buffer)
    if isinstance(model, Monster):
        _evolution(uid, "enlightening", model.enlightening, buffer)


def _element_records() -> tuple[list[StgElement], list[StgElementRelation]]:
    elements, relations = [], []
    for element in ELEMENT.values():
        elements.append(StgElement(element.id))
        for kind, related in (("effective", element.effective), ("weakness", element.weakness)):
            relations += [
                StgElementRelation(element.id, kind, ordinal, related_id)
                for ordinal, related_id in enumerate(related, 1)
            ]
    return elements, relations


def rows(models: Iterable[ParsedPage], *, run_id: str, parser_version: int) -> Records:
    """stage parsed pages into records"""
    pages = list(models)
    seen: set[str] = set()
    for page in pages:
        if page.source_id in seen:
            raise ValueError(f"repeated source_id {page.source_id!r}")
        seen.add(page.source_id)
        if (page.model is None) == (page.error is None):
            raise ValueError(f"ParsedPage {page.source_id!r} must carry exactly one of model and error")

    tables: dict[str, list] = {name: [] for name in STAGING_TABLES}
    parse_errors: list[ParseError] = []

    def reject(page: ParsedPage, error: str) -> None:
        parse_errors.append(ParseError(page.source_id, page.source_hash, parser_version, error, run_id))

    for page in sorted(pages, key=lambda p: p.source_id):
        if page.model is None:
            reject(page, page.error or "UnparseablePage: parse() returned no model")
            continue
        try:
            buffer = _page_records(page)
        except _PageRejected as e:
            reject(page, str(e))
            continue
        for name, records in buffer.items():
            tables[name] += records

    tables["stg_element"], tables["stg_element_relation"] = _element_records()
    return Records(run_id=run_id, parser_version=parser_version, **tables, parse_errors=parse_errors)
