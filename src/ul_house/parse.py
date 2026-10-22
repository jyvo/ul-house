from bs4 import BeautifulSoup

from ul_house.config import STAT_TIER_RE, EFFECT_SPLIT_RE, TARGET_RE, SECTION_RE, AMPLIFIER_RE, POTENTIAL_RE, SKILL_HEADING_RE
from ul_house.config import PASSIVE_HEADING, ABSENT_VAL, EFFECTS, ACTIVATION, ACTIVATION_RATE, SCALES, WEAPON_TYPES, MONSTER_TYPE
from ul_house.extract import fetch_name, fetch_data, fetch_stats, fetch_ability, fetch_skills, fetch_reforge, fetch_sp_evo

from ul_house.data.elements import ELEMENT
from ul_house.models.equipment import ItemRef, EquipmentRef, Weapon, DefensiveGear, Monster, Stats
from ul_house.models.gear_evolution import Reforge, Awakening, Enlightening, GearEvoMaterial, ItemEvoMaterial
from ul_house.models.gear_mechanism import Proc, ProcActivation, ProcScaling, SkillEffect, WeaponAbility, MonsterSkill, PassiveSkill, HiddenPotential, PotentialLevel


PARSER_VERSION: int = 1


class UnparseablePage(ValueError):
    """parse produced no model"""


def _to_int(value: str | int | None, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    digits = value.replace(",", "").strip()
    return int(digits) if digits.lstrip("-").isdigit() else default


def _lines(text: str) -> list[str]:
    """split effects one per line"""
    out = []
    for line in text.split("\n"):
        for part in EFFECT_SPLIT_RE.split(line):
            part = part.strip()
            if not part:
                continue
            # amplifier is its own effect
            if (match := AMPLIFIER_RE.search(part)) and part != match.group(1):
                out.append(part[:match.start()].strip())
                out.append(match.group(1))
            else:
                out.append(part)
    return out


def _sections(text: str) -> dict[str, list[str]]:
    """split proc cell on markers"""
    out: dict[str, list[str]] = {}
    current = EFFECTS
    for line in _lines(text):
        if match := SECTION_RE.match(line):
            current = match.group("name")
            out.setdefault(current, [])
        else:
            out.setdefault(current, []).append(line)
    return out


def _equip_ref(name: str | None, uid: str | None) -> EquipmentRef | None:
    """empty evolution slot check"""
    if not uid or not name or name == ABSENT_VAL:
        return None
    return EquipmentRef(uid=uid, name=name)


def _parse_effects(text: str) -> tuple[SkillEffect, ...]:
    effects = []
    for line in _lines(text):
        match = TARGET_RE.match(line)
        effects.append(SkillEffect(
            target=match.group("target") if match else None,
            description=match.group("description") if match else line,
        ))
    return tuple(effects)


def _parse_stats(soup: BeautifulSoup) -> tuple[Stats, ...]:
    stats = fetch_stats(soup)
    if not stats:
        return ()
    return tuple(
        Stats(label=label, values=tuple((STAT_TIER_RE.sub("", tier), value) for tier, value in values))
        for label, values in stats.items()
    )


def _parse_ability(soup: BeautifulSoup) -> WeaponAbility | None:
    ability = fetch_ability(soup)
    if not ability or not ability.get("uid"):
        return None
    return WeaponAbility(
        uid=ability["uid"],
        name=ability.get("name", ""),
        effect=_parse_effects(ability.get("effect", "")),
    )


def _parse_proc(soup: BeautifulSoup) -> Proc | None:
    for heading, value in fetch_skills(soup) or ():
        if not SKILL_HEADING_RE.match(heading) or not isinstance(value, tuple):
            continue

        name, text = value
        sections = _sections(text)
        effect_lines = sections.get(EFFECTS, [])
        condition = sections.get(ACTIVATION, [])
        rate_lines = sections.get(ACTIVATION_RATE, [])

        # scaling notes appear in both
        scaling = tuple(
            ProcScaling(description=line)
            for line in (*effect_lines, *rate_lines)
            if SCALES in line
        )
        rate = next((line for line in rate_lines if SCALES not in line), None)

        activation = None
        if condition or rate:
            activation = ProcActivation(condition=tuple(condition) or None, rate=rate)

        return Proc(
            name=name,
            effect=_parse_effects("\n".join(line for line in effect_lines if SCALES not in line)),
            activation=activation,
            scaling=scaling or None,
        )
    return None


def _parse_potential(soup: BeautifulSoup) -> HiddenPotential | None:
    unlocks, restrictions = [], None
    for heading, value in fetch_skills(soup) or ():
        if "restrictions" in heading and isinstance(value, str):
            restrictions = value
            continue
        if not isinstance(value, dict):
            continue

        for label, effect in value.items():
            if "restrictions" in label:
                restrictions = effect
            elif match := POTENTIAL_RE.match(label):
                unlocks.append(PotentialLevel(
                    level=int(match.group(1)),
                    effect=SkillEffect(description=effect),
                ))

    if not unlocks:
        return None
    return HiddenPotential(
        unlocks=tuple(sorted(unlocks, key=lambda unlock: unlock.level)),
        restrictions=restrictions,
    )


def _parse_skills(soup: BeautifulSoup, skill_type: str = "skill") -> tuple[MonsterSkill, ...] | PassiveSkill | HiddenPotential | None:
    if skill_type.startswith("hidden"):
        return _parse_potential(soup)

    if skill_type.startswith("passive"):
        for heading, value in fetch_skills(soup) or ():
            if heading == PASSIVE_HEADING and isinstance(value, tuple):
                return PassiveSkill(name=value[0], effect=_parse_effects(value[1]))
        return None

    # one or two skill headings
    return tuple(
        MonsterSkill(name=value[0], effect=_parse_effects(value[1]))
        for heading, value in fetch_skills(soup) or ()
        if SKILL_HEADING_RE.match(heading) and isinstance(value, tuple)
    )


def _parse_gear_materials(materials) -> tuple[GearEvoMaterial, ...] | None:
    """build reforge materials"""
    if not materials:
        return None
    return tuple(
        GearEvoMaterial(quantity=_to_int(material.get("quantity"), 1), gear=EquipmentRef(uid=uid, name=material.get("name", "")))
        for uid, material in materials.items()
        if uid
    ) or None


def _parse_reforge(soup: BeautifulSoup) -> Reforge | None:
    entries, materials = fetch_reforge(soup)

    before = None
    for entry in entries or ():
        for refs in entry.values():
            if ref := _equip_ref(refs.get("before"), refs.get("before_id")):
                if before is not None:
                    raise ValueError("two 'before reforge' slots")
                before = ref

    material = _parse_gear_materials(materials)
    if before is None and material is None:
        return None
    return Reforge(before=before, material=material)


def _parse_evo_materials(materials) -> tuple[tuple[GearEvoMaterial, ...] | None, tuple[ItemEvoMaterial, ...] | None]:
    """build special-evolution materials"""
    gear_materials = item_materials = None
    for block in materials or ():
        for heading, entries in block.items():
            if "gear" in heading:
                gear_materials = tuple(
                    GearEvoMaterial(quantity=_to_int(quantity, 1), gear=EquipmentRef(uid=uid, name=name))
                    for uid, (name, quantity) in entries.items()
                ) or None
            elif "item" in heading:
                item_materials = tuple(
                    ItemEvoMaterial(quantity=_to_int(quantity, 1), item=ItemRef(uid=uid, name=name))
                    for uid, (name, quantity) in entries.items()
                ) or None
    return gear_materials, item_materials


def _parse_sp_evo(soup: BeautifulSoup) -> dict[str, Awakening | Enlightening | None]:
    """build awakening or enlightening"""
    entries, materials = fetch_sp_evo(soup)
    evolutions: dict[str, Awakening | Enlightening | None] = {"awakening": None, "enlightening": None}
    if not entries:
        return evolutions

    refs: dict[str, dict[str, EquipmentRef]] = {}
    for entry in entries:
        for heading, data in entry.items():
            kind = next((k for k in evolutions if k in heading), None)
            if kind is None:
                continue
            sides = refs.setdefault(kind, {})
            if ref := _equip_ref(data.get("before"), data.get("before_id")):
                if "before" in sides:
                    raise ValueError(f"two 'before {kind}' slots")
                sides["before"] = ref

    gear_materials, item_materials = _parse_evo_materials(materials)
    for kind, sides in refs.items():
        model = Awakening if kind == "awakening" else Enlightening
        evolutions[kind] = model(
            before=sides.get("before"),
            gear_materials=gear_materials,
            item_materials=item_materials,
        )
    return evolutions


def parse(soup: BeautifulSoup, item_id: str) -> Weapon | DefensiveGear | Monster | None:
    name = fetch_name(soup)

    basic_data = fetch_data(soup)
    if basic_data is None:
        return None
    
    rarity = basic_data.get("rarity", "")
    gear_type = basic_data.get("gear type", "")
    cost = _to_int(basic_data.get("gear cost"))
    element = ELEMENT.get(basic_data.get("element", ""))
    infusion_count = _to_int(basic_data.get("infusion count"))
    max_level = _to_int(basic_data.get("max level"))

    stats = _parse_stats(soup)
    reforge = _parse_reforge(soup)

    sp_evo = _parse_sp_evo(soup)
    awakening = sp_evo.get("awakening")
    enlightening = sp_evo.get("enlightening")

    common = dict(
        uid=item_id,
        name=name,
        rarity=rarity,
        gear_type=gear_type,
        cost=cost,
        element=element,
        max_level=max_level,
        stats=stats,
        reforge=reforge,
        awakening=awakening,
    )

    if gear_type in WEAPON_TYPES:
        return Weapon(
            **common,
            infusion_count=infusion_count,
            skill=_parse_proc(soup),
            weapon_ability=_parse_ability(soup),
        )

    if gear_type == MONSTER_TYPE:
        return Monster(
            **common,
            skill=_parse_skills(soup),
            passive=_parse_skills(soup, "passive"),
            hidden_potential=_parse_skills(soup, "hidden"),
            enlightening=enlightening,
        )

    if gear_type:
        return DefensiveGear(
            **common,
            infusion_count=infusion_count,
            skill=_parse_proc(soup),
        )
    return None


def parse_html(html: str | bytes, item_id: str) -> Weapon | DefensiveGear | Monster:
    model = parse(BeautifulSoup(html, "lxml"), item_id)
    if model is None:
        raise UnparseablePage(f"{item_id}: no basic data table or no gear type")
    return model
