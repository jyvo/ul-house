from __future__ import annotations
from dataclasses import dataclass
from abc import ABC

from models.equipment import EquipmentRef, ItemRef


@dataclass(frozen=True, slots=True)
class GearEvolution(ABC):
    before: EquipmentRef | None
    after: EquipmentRef | None


@dataclass(frozen=True, slots=True)
class Reforge(GearEvolution):
    material: tuple[GearEvoMaterial, ...] | None


@dataclass(frozen=True, slots=True)
class SpecailEvolution(GearEvolution, ABC):
    gear_materials: tuple[GearEvoMaterial, ...] | None
    item_materials: tuple[ItemEvoMaterial, ...] | None
    

@dataclass(frozen=True, slots=True)
class Awakening(SpecailEvolution):
    pass


@dataclass(frozen=True, slots=True)
class Enlightening(SpecailEvolution):
    pass


@dataclass(frozen=True, slots=True)
class EvoMaterial(ABC):
    quantity: int


@dataclass(frozen=True, slots=True)
class GearEvoMaterial(EvoMaterial):
    gear: EquipmentRef


@dataclass(frozen=True, slots=True)
class ItemEvoMaterial(EvoMaterial):
    item: ItemRef
