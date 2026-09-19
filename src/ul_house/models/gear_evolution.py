from __future__ import annotations
from dataclasses import dataclass
from abc import ABC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ul_house.models.equipment import EquipmentRef, ItemRef


@dataclass(frozen=True, slots=True)
class GearEvolution(ABC):
    before: EquipmentRef | None = None
    after: EquipmentRef | None = None


@dataclass(frozen=True, slots=True)
class Reforge(GearEvolution):
    material: tuple[GearEvoMaterial, ...] | None = None


@dataclass(frozen=True, slots=True)
class SpecailEvolution(GearEvolution, ABC):
    gear_materials: tuple[GearEvoMaterial, ...] | None = None
    item_materials: tuple[ItemEvoMaterial, ...] | None = None


@dataclass(frozen=True, slots=True)
class Awakening(SpecailEvolution):
    pass


@dataclass(frozen=True, slots=True)
class Enlightening(SpecailEvolution):
    pass


@dataclass(frozen=True, slots=True)
class EvoMaterial(ABC):
    quantity: int = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class GearEvoMaterial(EvoMaterial):
    gear: EquipmentRef


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemEvoMaterial(EvoMaterial):
    item: ItemRef
