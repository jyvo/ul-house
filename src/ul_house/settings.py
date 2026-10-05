"""build-scope and app settings (crawl specs)"""
from __future__ import annotations

import hashlib
import json
import math
import tomllib
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from ul_house.config import EVO_KIND, RARITY_LIST_PAGE

CATALOG_PATH = Path(__file__).with_name("catalog.toml")

EVO_KINDS = tuple(EVO_KIND.values())                     # reforge, awakening, enlightening
DIRECTIONS = ("predecessors", "successors", "both")
POLICIES = ("exclude", "reference", "catalog")
RARITIES = tuple(RARITY_LIST_PAGE.values())
TOP_LEVEL = ""


class ScopeError(ValueError):
    pass


# read table
def _table(data: Any, where: str, required: set[str], optional: set[str] = frozenset()) -> Mapping[str, Any]:
    label = f"[{where}]" if where else "top level"      # top level of catalog.toml is not a table
    if not isinstance(data, Mapping):
        raise ScopeError(f"{label} must be a table")
    unknown = set(data) - required - optional
    if unknown:
        raise ScopeError(f"{label} unknown key(s): {', '.join(sorted(unknown))}")
    missing = required - set(data)
    if missing:
        raise ScopeError(f"{label} missing key(s): {', '.join(sorted(missing))}")
    return data


def _is_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScopeError(f"{where} must be an integer, got {value!r}")
    return value


def _is_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ScopeError(f"{where} must be a finite number, got {value!r}")
    return float(value)


def _is_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ScopeError(f"{where} must be true or false, got {value!r}")
    return value


def _is_str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ScopeError(f"{where} must be a string, got {value!r}")
    return value


def _is_strs(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ScopeError(f"{where} must be a list of strings, got {value!r}")
    return tuple(value)


def _one_of(value: str, allowed: tuple[str, ...], where: str) -> str:
    if value not in allowed:
        raise ScopeError(f"{where} must be one of {', '.join(allowed)}; got {value!r}")
    return value


# tables
@dataclass(frozen=True)
class CostBand:
    min: int
    max: int
    cosmetic: int   # 99c, placeholder cost for cosmetics + mats + non-catalog items

    def __post_init__(self):
        if not 0 <= self.min <= self.max < self.cosmetic:
            raise ScopeError(f"cost needs 0 <= min <= max < cosmetic; got {self.min}, {self.max}, {self.cosmetic}")

    @classmethod
    def from_mapping(cls, data) -> CostBand:
        t = _table(data, "cost", {"min", "max", "cosmetic"})
        return cls(_is_int(t["min"], "cost.min"), _is_int(t["max"], "cost.max"), _is_int(t["cosmetic"], "cost.cosmetic"))

    def contains(self, cost: int) -> bool:
        return cost != self.cosmetic and self.min <= cost <= self.max


@dataclass(frozen=True)
class RarityRule:
    include: tuple[str, ...]

    def __post_init__(self):
        if not self.include:
            raise ScopeError("rarity.include must name at least one rarity")
        for rarity in self.include:
            _one_of(rarity, RARITIES, "rarity.include")
        if len(set(self.include)) != len(self.include):
            raise ScopeError("rarity.include has duplicates")

    @classmethod
    def from_mapping(cls, data) -> RarityRule:
        t = _table(data, "rarity", {"include"})
        return cls(_is_strs(t["include"], "rarity.include"))


@dataclass(frozen=True)
class NameRules:
    """tokens matched as case-insensitive substrings of the item name (specifically for progression items and fodders)"""

    excluded: tuple[str, ...] = ()
    bypass: tuple[str, ...] = ()

    def __post_init__(self):
        for token in self.excluded + self.bypass:
            if not token.strip():
                raise ScopeError("names tokens must not be blank")
            if token != token.casefold():
                raise ScopeError(f"names token {token!r} must be lowercase")
        overlap = set(self.excluded) & set(self.bypass)
        if overlap:
            raise ScopeError(f"names.bypass and names.excluded overlap: {', '.join(sorted(overlap))}")

    @classmethod
    def from_mapping(cls, data) -> NameRules:
        t = _table(data, "names", set(), {"excluded", "bypass"})
        return cls(_is_strs(t.get("excluded", []), "names.excluded"), _is_strs(t.get("bypass", []), "names.bypass"))

    @staticmethod
    def _hit(name: str, tokens: tuple[str, ...]) -> str | None:
        folded = name.casefold()
        return next((token for token in tokens if token in folded), None)

    def excluded_token(self, name: str) -> str | None:
        return self._hit(name, self.excluded)

    def bypass_token(self, name: str) -> str | None:
        return self._hit(name, self.bypass)


@dataclass(frozen=True)
class ChainRule:
    policy: str | None = None
    depth: int | None = None

    def __post_init__(self):
        if self.policy is not None:
            _one_of(self.policy, POLICIES, "evolution.per_chain.policy")
        if self.depth is not None and self.depth < 0:
            raise ScopeError("evolution.per_chain.depth must be >= 0")


@dataclass(frozen=True)
class EvolutionRule:
    follow: tuple[str, ...]
    direction: str
    depth: int
    policy: str
    override_filters: bool
    per_chain: tuple[tuple[str, ChainRule], ...] = ()

    def __post_init__(self):
        for kind in self.follow:
            _one_of(kind, EVO_KINDS, "evolution.follow")
        if len(set(self.follow)) != len(self.follow):
            raise ScopeError("evolution.follow has dupes")
        _one_of(self.direction, DIRECTIONS, "evolution.direction")
        _one_of(self.policy, POLICIES, "evolution.policy")
        if self.depth < 0:
            raise ScopeError("evolution.depth must be >= 0")
        for kind, _ in self.per_chain:
            if kind not in self.follow:
                raise ScopeError(f"evolution.per_chain.{kind} is not in evolution.follow")
        if self.follow and self.max_depth == 0:
            warnings.warn("evolution.follow is set but every depth is 0; no chain will be walked", stacklevel=2)

    @classmethod
    def from_mapping(cls, data) -> EvolutionRule:
        t = _table(data, "evolution", {"follow", "direction", "depth", "policy", "override_filters"}, {"per_chain"})
        chains = _table(t.get("per_chain", {}), "evolution.per_chain", set(), set(EVO_KINDS))
        per_chain = []
        for kind in sorted(chains):
            where = f"evolution.per_chain.{kind}"
            c = _table(chains[kind], where, set(), {"policy", "depth"})
            per_chain.append((kind, ChainRule(
                _is_str(c["policy"], f"{where}.policy") if "policy" in c else None,
                _is_int(c["depth"], f"{where}.depth") if "depth" in c else None,
            )))
        return cls(
            follow=_is_strs(t["follow"], "evolution.follow"),
            direction=_is_str(t["direction"], "evolution.direction"),
            depth=_is_int(t["depth"], "evolution.depth"),
            policy=_is_str(t["policy"], "evolution.policy"),
            override_filters=_is_bool(t["override_filters"], "evolution.override_filters"),
            per_chain=tuple(per_chain),
        )

    def _chain(self, kind: str) -> ChainRule:
        return dict(self.per_chain).get(kind, ChainRule())

    def depth_for(self, kind: str) -> int:
        if kind not in self.follow:
            return 0
        chain = self._chain(kind)
        return self.depth if chain.depth is None else chain.depth

    def policy_for(self, kind: str) -> str:
        chain = self._chain(kind)
        return self.policy if chain.policy is None else chain.policy

    @property
    def max_depth(self) -> int:
        return max((self.depth_for(kind) for kind in self.follow), default=0)


@dataclass(frozen=True)
class CrawlConfig:
    request_interval: float
    max_retries: int
    connect_timeout: float
    read_timeout: float
    request_deadline: float

    def __post_init__(self):
        if self.request_interval <= 0:
            raise ScopeError("crawl.request_interval must be > 0")
        if self.max_retries < 0:
            raise ScopeError("crawl.max_retries must be >= 0")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ScopeError("crawl timeouts must be > 0")
        if self.request_deadline < self.read_timeout:
            raise ScopeError("crawl.request_deadline must be >= crawl.read_timeout")

    @classmethod
    def from_mapping(cls, data) -> CrawlConfig:
        t = _table(data, "crawl", {f.name for f in fields(cls)})
        return cls(
            request_interval=_is_float(t["request_interval"], "crawl.request_interval"),
            max_retries=_is_int(t["max_retries"], "crawl.max_retries"),
            connect_timeout=_is_float(t["connect_timeout"], "crawl.connect_timeout"),
            read_timeout=_is_float(t["read_timeout"], "crawl.read_timeout"),
            request_deadline=_is_float(t["request_deadline"], "crawl.request_deadline"),
        )

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)


@dataclass(frozen=True)
class RevalidateConfig:
    cycle_days: int         # daily run revalidates 1/cycle_days of the catalog

    def __post_init__(self):
        if self.cycle_days < 1:
            raise ScopeError("revalidate.cycle_days must be >= 1")

    @classmethod
    def from_mapping(cls, data) -> RevalidateConfig:
        t = _table(data, "revalidate", {"cycle_days"})
        return cls(_is_int(t["cycle_days"], "revalidate.cycle_days"))


# scope/baseline/specifications
@dataclass(frozen=True)
class Scope:
    cost: CostBand
    rarity: RarityRule
    names: NameRules
    evolution: EvolutionRule
    crawl: CrawlConfig
    revalidate: RevalidateConfig

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Scope:
        t = _table(data, TOP_LEVEL, {"cost", "rarity", "names", "evolution", "crawl", "revalidate"})
        return cls(
            cost=CostBand.from_mapping(t["cost"]),
            rarity=RarityRule.from_mapping(t["rarity"]),
            names=NameRules.from_mapping(t["names"]),
            evolution=EvolutionRule.from_mapping(t["evolution"]),
            crawl=CrawlConfig.from_mapping(t["crawl"]),
            revalidate=RevalidateConfig.from_mapping(t["revalidate"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        """TOML: from_mapping(to_mapping()) == self"""
        evo = self.evolution
        per_chain = {
            kind: {k: v for k, v in (("policy", rule.policy), ("depth", rule.depth)) if v is not None}
            for kind, rule in evo.per_chain
        }
        evolution = {
            "follow": list(evo.follow), "direction": evo.direction, "depth": evo.depth,
            "policy": evo.policy, "override_filters": evo.override_filters,
        }
        if per_chain:
            evolution["per_chain"] = per_chain
        return {
            "cost": {"min": self.cost.min, "max": self.cost.max, "cosmetic": self.cost.cosmetic},
            "rarity": {"include": list(self.rarity.include)},
            "names": {"excluded": list(self.names.excluded), "bypass": list(self.names.bypass)},
            "evolution": evolution,
            "crawl": {f.name: getattr(self.crawl, f.name) for f in fields(self.crawl)},
            "revalidate": {"cycle_days": self.revalidate.cycle_days},
        }

    def stamp(self) -> str:
        """JSON, what seed.sqlite and app.sqlite record in meta"""
        return json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_stamp(cls, stamp: str) -> Scope:
        return cls.from_mapping(json.loads(stamp))

    def fingerprint(self) -> str:
        mapping = self.to_mapping()
        selection = {key: mapping[key] for key in ("cost", "rarity", "names", "evolution")}
        return hashlib.sha256(json.dumps(selection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_scope(path: str | Path = CATALOG_PATH) -> Scope:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ScopeError(f"{path}: no such catalog file") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ScopeError(f"{path}: {exc}") from exc
    return Scope.from_mapping(data)


# app settings
@dataclass(frozen=True)
class UserSettings:
    cost_min: int
    cost_max: int
    rarity_include: tuple[str, ...]
    names_excluded: tuple[str, ...] = ()
    evolution_policy: str = "reference"

    def __post_init__(self):
        _is_int(self.cost_min, "cost_min")
        _is_int(self.cost_max, "cost_max")
        if self.cost_min > self.cost_max:
            raise ScopeError(f"cost_min {self.cost_min} is above cost_max {self.cost_max}")
        if not self.rarity_include:
            raise ScopeError("rarity_include must name at least one rarity")
        _one_of(self.evolution_policy, POLICIES, "evolution_policy")

    @classmethod
    def from_scope(cls, scope: Scope) -> UserSettings:
        return cls(scope.cost.min, scope.cost.max, scope.rarity.include,
                   scope.names.excluded, scope.evolution.policy)


@dataclass(frozen=True)
class ScopeRestriction:
    settings: UserSettings
    widened: tuple[str, ...] = field(default=())


def clamp(wanted: UserSettings, scope: Scope) -> ScopeRestriction:
    """anything outside the scope is not in app.sqlite"""
    widened = []

    def into_band(value: int) -> int:
        return min(max(value, scope.cost.min), scope.cost.max)

    cost_min, cost_max = into_band(wanted.cost_min), into_band(wanted.cost_max)
    if cost_min != wanted.cost_min:
        widened.append("cost_min")
    if cost_max != wanted.cost_max:
        widened.append("cost_max")

    if set(wanted.rarity_include) - set(scope.rarity.include):
        widened.append("rarity_include")
    rarity = tuple(r for r in scope.rarity.include if r in wanted.rarity_include) or scope.rarity.include

    wanted_excluded = tuple(token.casefold() for token in wanted.names_excluded)
    excluded = tuple(dict.fromkeys(scope.names.excluded + wanted_excluded))

    policy = wanted.evolution_policy
    if POLICIES.index(policy) > POLICIES.index(scope.evolution.policy):
        widened.append("evolution_policy")
        policy = scope.evolution.policy

    settings = replace(wanted, cost_min=cost_min, cost_max=cost_max, rarity_include=rarity,
                       names_excluded=excluded, evolution_policy=policy)
    return ScopeRestriction(settings, tuple(widened))
