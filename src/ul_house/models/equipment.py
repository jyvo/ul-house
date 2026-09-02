from __future__ import annotations
from dataclasses import dataclass
from abc import ABC

from ul_house.models.combat_mechanism import Element
from ul_house.models.gear_mechanism import Proc, WeaponAbility, MonsterSkill, PassiveSkill, HiddenPotential
from ul_house.models.gear_evolution import Reforge, Awakening, Enlightening


@dataclass(frozen=True, slots=True)
class ItemRef:
    id: str
    icon_url: str
    name: str


@dataclass(frozen=True, slots=True)
class EquipmentRef(ItemRef):
    details_url: str


@dataclass(frozen=True, slots=True)
class Equipment(EquipmentRef):
    rarity: str
    gear_type: str
    cost: int
    element: Element | None
    max_level: int
    stats: tuple[Stats, ...]
    reforge: Reforge
    awakening: Awakening | None


@dataclass(frozen=True, slots=True)
class Weapon(Equipment):
    infusion_count: int
    skill: Proc
    weapon_ability: WeaponAbility | None


@dataclass(frozen=True, slots=True)
class DefensiveGear(Equipment):
    infusion_count: int
    skill: Proc


@dataclass(frozen=True, slots=True)
class Monster(Equipment):
    hidden_potential_count: int
    skill: tuple[MonsterSkill, MonsterSkill | None]
    passive: PassiveSkill | None
    hidden_potential: HiddenPotential | None
    enlightening: Enlightening | None


@dataclass(frozen=True, slots=True)
class Stats:
    label: str      # stat label/name
    tier: str       # initial, max1, max2
    value: int      # value w.r. to tier
    slotted: bool   # whether label is set or not
