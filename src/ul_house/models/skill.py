from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class Skill:
   name: str
   effect: tuple[str, ...]
