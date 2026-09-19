from __future__ import annotations
from dataclasses import dataclass
from abc import ABC

from ul_house.models.combat_mechanism import Element
from ul_house.models.gear_mechanism import Proc, WeaponAbility, MonsterSkill, PassiveSkill, HiddenPotential
from ul_house.models.gear_evolution import Reforge, Awakening, Enlightening
from ul_house.config import BASE_URL

@dataclass(frozen=True, slots=True)
class ItemRef:
    uid: str
    name: str

    @property
    def icon_url(self) -> str:
        return f"{BASE_URL}/images/itemicon/item_{self.uid}.png"


@dataclass(frozen=True, slots=True)
class EquipmentRef(ItemRef):
    @property
    def info_url(self) -> str:
        return f"{BASE_URL}/en/equip_detail/{self.uid}.html"

    @property
    def icon_url(self) -> str:
        return f"{BASE_URL}/images/equipicon/{self.uid}.png"


@dataclass(frozen=True, slots=True)
class Equipment(EquipmentRef, ABC):
    rarity: str
    gear_type: str
    cost: int
    element: Element
    max_level: int
    stats: tuple[Stats, ...]
    reforge: Reforge | None = None # None if before and after are not present via parser, note table always present
    awakening: Awakening | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Weapon(Equipment):
    infusion_count: int = 0
    skill: Proc
    weapon_ability: WeaponAbility | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DefensiveGear(Equipment):
    infusion_count: int = 0
    skill: Proc


@dataclass(frozen=True, slots=True, kw_only=True)
class Monster(Equipment):
    skill: tuple[MonsterSkill, ...]
    passive: PassiveSkill | None = None
    hidden_potential: HiddenPotential | None = None
    enlightening: Enlightening | None = None

    @property
    def hidden_potential_count(self) -> int:
        return self.hidden_potential.max_level if self.hidden_potential is not None else 0


@dataclass(frozen=True, slots=True)
class Stats:
    label: str      # stat label/name
    values: tuple[tuple[str, int], ...]       # tier: value, e.g. ("initial", 100)

    # change slotted as a property call
    @property
    def slotted(self) -> bool:
        return "stat" not in self.label.casefold()

    