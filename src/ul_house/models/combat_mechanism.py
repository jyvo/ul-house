from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class Element:
    id: str
    effective: tuple[str, ...]
    weakness: tuple[str, ...]
