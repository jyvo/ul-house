"""hash ids"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

SEPARATOR: str = "\x1f"
RECORD_SEPARATOR: str = "\x1e"
ID_MASK: int = 0x7FFF_FFFF_FFFF_FFFF

_ID_TEXT_RE = re.compile(r"^(0|[1-9][0-9]*)$")
_ID_LIMIT = 2**63


def _render(field: str | int | None) -> str:
    # bool is an int subclass -> reject it explicitly
    if field is None:
        return ""
    if isinstance(field, bool):
        raise TypeError(f"hash_id field must be str, int or None, not {type(field).__name__}")
    if isinstance(field, str):
        if SEPARATOR in field:
            raise ValueError("hash_id field contains U+001F")
        return field
    if isinstance(field, int):
        return str(field)
    raise TypeError(f"hash_id field must be str, int or None, not {type(field).__name__}")


def canonical_key(*fields: str | int | None) -> bytes:
    if not fields:
        raise TypeError("hash_id needs at least one field")
    return SEPARATOR.join(_render(field) for field in fields).encode("utf-8")


def hash_id(*fields: str | int | None) -> int:
    digest = hashlib.sha256(canonical_key(*fields)).digest()
    return int.from_bytes(digest[:8], "big") & ID_MASK


def family_id(name: str) -> int:
    """proc_family.family_id"""
    return hash_id(name)


def proc_id(raw_name: str, effect_hash: str) -> int:
    """proc.proc_id"""
    return hash_id(raw_name, effect_hash)


def effect_id(target: str | None, description: str) -> int:
    """skill_effect.effect_id"""
    return hash_id(target, description)


def passive_id(name: str, effect_hash: str) -> int:
    """passive_skill.passive_id"""
    return hash_id(name, effect_hash)


def skill_id(uid: str, ordinal: int) -> int:
    """monster_skill.skill_id"""
    return hash_id(uid, ordinal)


def _checked_line(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError(f"effect_hash input must be str, not {type(text).__name__}")
    if SEPARATOR in text or RECORD_SEPARATOR in text:
        raise ValueError("effect_hash input contains U+001E or U+001F")
    return text


def effect_hash(effects: Iterable[tuple[str | None, str]], conditions: Iterable[str] = (), rate: str | None = None, scaling: Iterable[str] = ()) -> str:
    """content hash of combat mech parsed text"""
    lines: list[str] = []
    for target, description in effects:
        target_text = "" if target is None else _checked_line(target)
        lines.append(SEPARATOR.join(("e", target_text, _checked_line(description))))
    for condition in conditions:
        lines.append(SEPARATOR.join(("c", _checked_line(condition))))
    if rate is not None:
        lines.append(SEPARATOR.join(("r", _checked_line(rate))))
    for description in scaling:
        lines.append(SEPARATOR.join(("s", _checked_line(description))))
    return hashlib.sha256(RECORD_SEPARATOR.join(lines).encode("utf-8")).hexdigest()


def id_to_text(value: int) -> str:
    """decimal text of a hashed id"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"id must be int, not {type(value).__name__}")
    if not 0 <= value < _ID_LIMIT:
        raise ValueError(f"id {value} outside [0, 2**63)")
    return str(value)


def id_from_text(text: str) -> int:
    """no sign, no whitespace, no leading zeros, no '+' """
    if not isinstance(text, str):
        raise TypeError(f"id text must be str, not {type(text).__name__}")
    if _ID_TEXT_RE.fullmatch(text) is None or not text.isascii():
        raise ValueError(f"not a hashed id: {text!r}")
    value = int(text)
    if value >= _ID_LIMIT:
        raise ValueError(f"id {text} outside [0, 2**63)")
    return value
