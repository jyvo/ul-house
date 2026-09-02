from __future__ import annotations
from dataclasses import dataclass
from abc import ABC


@dataclass(frozen=True, slots=True)
class CombatMech(ABC):
    name: str
    effect: tuple[SkillEffect, ...]


@dataclass(frozen=True, slots=True)
class Proc(CombatMech): # shared/similar skills with same types (weapons, def/mdef)
    pass


@dataclass(frozen=True, slots=True)
class WeaponAbility(CombatMech):
    id: str
    icon_url: str
    

@dataclass(frozen=True, slots=True)
class MonsterSkill(CombatMech): # unique per monster, but have shared SkillEffects
    pass


@dataclass(frozen=True, slots=True)
class PassiveSkill(CombatMech): # monster passive
    pass


@dataclass(slots=True)
class HiddenPotential:
    unlocks: tuple[PotentialLevel, ...]
    unlocked_level: int = 0 # needs to be mutable for tracking
    restrictions: str | None

    @property
    def max_level(self) -> int:
        return len(self.unlocks)

    @property
    def active_effects(self) -> tuple[SkillEffect, ...]:
        return tuple(unlock.effect for unlock in self.unlocks if unlock.level <= self.unlocked_level)

    def unlock(self, level: int) -> None:
        if 0 <= level <= self.max_level:
            self.unlocked_level = level
        else:
            raise ValueError(f"Invalid level {level}. Must be between 0 and {self.max_level}.")


@dataclass(frozen=True, slots=True)
class PotentialLevel:
    level: int
    effect: SkillEffect


# for now assume this basic structure, doc as abstract later (ABC)
@dataclass(frozen=True, slots=True)
class SkillEffect:
    target: str | None # single or multi target
    description: str


# skip these for now, determine after full crawl
@dataclass(frozen=True, slots=True)
class DamageEffect(SkillEffect):
    damage_type: str # physical or magic
    ability_power: int
    success_value: int


@dataclass(frozen=True, slots=True)
class BuffEffect(SkillEffect):
    buff_type: str # attack, defense, speed, etc.
    buff_value: int
    duration: int # in turns
