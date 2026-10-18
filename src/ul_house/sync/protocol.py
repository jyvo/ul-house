"""frozen dataclasses mirroring api/contract/schemas/*.schema.json"""
from __future__ import annotations

import dataclasses
import functools
import json
import re
import types
import typing
from datetime import datetime, timezone
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Union

from ul_house.database import entities
from ul_house.sync.errors import ProtocolError

API_SCHEMA_VERSION = 1

KEY_PATTERNS: dict[str, str] = {
    "wiki_uid": r"^[0-9]{1,20}$",
    "hash_id": r"^(0|[1-9][0-9]{0,18})$",
    "sha256": r"^[0-9a-f]{64}$",
    "element_id": r"^[a-z]{1,16}$",
}
ICON_KINDS: tuple[str, ...] = ("equipment", "item", "ability")
SHA256_RE = re.compile(KEY_PATTERNS["sha256"])
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_REVISION = 999_999_999_999_999

#: entity types in packages (= database.entities.ENTITIES, 11 names)
ENTITY_TYPES: tuple[str, ...] = tuple(e.entity_type for e in entities.ENTITIES)
DELETABLE_ENTITIES: tuple[str, ...] = tuple(e.entity_type for e in entities.ENTITIES if e.deletable)
#: which KEY_PATTERNS entry validates an op's key, per entity type
ENTITY_KEY_PATTERN: dict[str, str] = {
    "equipment": "wiki_uid", "item": "wiki_uid", "weapon_ability": "wiki_uid",
    "uid_retired": "wiki_uid", "uid_alias": "wiki_uid",
    "proc_family": "hash_id", "proc": "hash_id", "skill_effect": "hash_id", "passive_skill": "hash_id",
    "icon": "sha256", "element": "element_id",
}
RETIRE_REASONS: tuple[str, ...] = ("gone", "out_of_scope", "downgraded")
REQUIRES_SNAPSHOT_REASONS: tuple[str, ...] = ("below_minimum", "package_missing")
SNAPSHOT_ONLY_REASONS: tuple[str, ...] = (
    "package_size", "dataset_schema_version", "catalog_version", "forced", "first_release", "no_base")
ICON_PACK_NAME_RE = re.compile(r"^(base-[1-9][0-9]{0,14}|delta-[1-9][0-9]{0,14}-[1-9][0-9]{0,14})\.zip$")

Scalar = str | int | float | None
Row = Mapping[str, Scalar]

_ROW_COLUMN_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def utc_now() -> datetime:
    """the current time as an aware UTC datetime (injectable `now` parameters default to this)"""
    return datetime.now(timezone.utc)


def format_utc(moment: datetime) -> str:
    """ISO-8601 UTC with a Z suffix, whole seconds (P10)"""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reject_constant(name: str) -> Any:
    raise ProtocolError(f"non-finite number {name} is not allowed")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ProtocolError(f"duplicate key {key!r}")
        out[key] = value
    return out


def loads(raw: bytes | str) -> dict[str, Any]:
    try:
        doc = json.loads(raw, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except ProtocolError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ProtocolError("top level must be a JSON object")
    return doc


_INT_RANGES: dict[str, tuple[int, int | None]] = {
    "revision": (1, MAX_REVISION), "from_revision": (1, MAX_REVISION), "to_revision": (1, MAX_REVISION),
    "minimum_revision": (1, MAX_REVISION), "snapshot_revision": (1, MAX_REVISION), "rev": (1, MAX_REVISION),
    "dataset_schema_version": (1, 65535), "api_schema_version": (1, 65535),
    "bytes": (1, None), "icon_count": (0, None), "count": (0, None), "parser_version": (1, None),
}
_INT_OVERRIDES: dict[tuple[str, str], tuple[int, int | None]] = {
    ("RequiresSnapshot", "from_revision"): (0, MAX_REVISION),
    ("DeltaPackRef", "icon_count"): (1, None),
}

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z.+_-]{1,64}$")
_COMMIT_FIELDS = {"build_commit", "catalog_commit", "policy_commit", "confirmed_commit"}
_PATTERNS: dict[tuple[str, str], re.Pattern] = {
    ("Current", "manifest"): re.compile(r"^manifests/[1-9][0-9]{0,14}\.json$"),
    ("Current", "sig"): re.compile(r"^[A-Za-z0-9+/]{86}==$"),
    ("SnapshotRef", "path"): re.compile(r"^snapshots/unison-[1-9][0-9]{0,14}\.sqlite\.gz$"),
    ("UpdateRef", "path"): re.compile(r"^updates/[1-9][0-9]{0,14}-[1-9][0-9]{0,14}\.json\.gz$"),
    ("BasePackRef", "path"): re.compile(r"^icons/packs/base-[1-9][0-9]{0,14}\.zip$"),
    ("DeltaPackRef", "path"): re.compile(r"^icons/packs/delta-[1-9][0-9]{0,14}-[1-9][0-9]{0,14}\.zip$"),
    ("LineageRef", "path"): re.compile(r"^lineage/[1-9][0-9]{0,14}\.json\.gz$"),
    ("IconDelta", "pack"): re.compile(r"^icons/packs/delta-[1-9][0-9]{0,14}-[1-9][0-9]{0,14}\.zip$"),
    ("IconPackListing", "name"): ICON_PACK_NAME_RE,
    ("IconPackEntry", "path"): re.compile(r"^(equipment|item|ability)/[0-9a-f]{64}\.png$"),
    ("Build", "transform_run_id"): _RUN_ID_RE,
    ("Build", "crawl_run_id"): _RUN_ID_RE,
    ("Runtime", "python"): _VERSION_RE, ("Runtime", "duckdb"): _VERSION_RE,
    ("Runtime", "dbt_core"): _VERSION_RE, ("Runtime", "dbt_duckdb"): _VERSION_RE,
    ("GateFinding", "check"): re.compile(r"^[a-z][a-z0-9_]{0,63}$"),
    ("GateFinding", "scope"): re.compile(r"^[a-z][a-z0-9_]{0,63}$"),
}
_SHA_ITEM_FIELDS = {("IconDelta", "added"), ("ManifestIcons", "withdrawn")}


def _check_str(cls: str, name: str, value: str) -> None:
    pattern = _PATTERNS.get((cls, name))
    if pattern is None:
        if name.endswith("sha256") or name in ("policy_version", "catalog_version"):
            pattern = SHA256_RE
        elif name.endswith("_at"):
            pattern = UTC_RE
        elif name in _COMMIT_FIELDS:
            pattern = COMMIT_RE
    if pattern is not None and not pattern.fullmatch(value):
        raise ProtocolError(f"{cls}.{name}: {value[:80]!r} does not match {pattern.pattern}")


def _check_int(cls: str, name: str, value: int) -> None:
    low_high = _INT_OVERRIDES.get((cls, name)) or _INT_RANGES.get(name)
    if low_high is None:
        return
    low, high = low_high
    if value < low or (high is not None and value > high):
        raise ProtocolError(f"{cls}.{name}: {value} is out of range")


@functools.lru_cache(maxsize=None)
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _is_union(tp: Any) -> bool:
    return typing.get_origin(tp) in (Union, types.UnionType)


def _decode(tp: Any, value: Any, cls: str, name: str) -> Any:
    origin = typing.get_origin(tp)
    if origin is Literal:
        options = typing.get_args(tp)
        if not any(value == option and type(value) is type(option) for option in options):
            raise ProtocolError(f"{cls}.{name}: {value!r} is not one of {options}")
        return value
    if _is_union(tp):
        args = typing.get_args(tp)
        if value is None and type(None) in args:
            return None
        members = [a for a in args if a is not type(None)]
        if len(members) != 1:
            raise ProtocolError(f"{cls}.{name}: unsupported union")
        return _decode(members[0], value, cls, name)
    if tp is bool:
        if not isinstance(value, bool):
            raise ProtocolError(f"{cls}.{name}: expected boolean")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError(f"{cls}.{name}: expected integer")
        _check_int(cls, name, value)
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProtocolError(f"{cls}.{name}: expected number")
        return float(value) if isinstance(value, int) else value
    if tp is str:
        if not isinstance(value, str):
            raise ProtocolError(f"{cls}.{name}: expected string")
        _check_str(cls, name, value)
        return value
    if origin is tuple:
        item = typing.get_args(tp)[0]
        if not isinstance(value, list):
            raise ProtocolError(f"{cls}.{name}: expected array")
        out = tuple(_decode(item, v, cls, name) for v in value)
        if (cls, name) in _SHA_ITEM_FIELDS:
            for v in out:
                if not isinstance(v, str) or not SHA256_RE.fullmatch(v):
                    raise ProtocolError(f"{cls}.{name}: {v!r} is not a sha256")
        return out
    if origin in (dict, Mapping):
        key_tp, val_tp = typing.get_args(tp)
        if not isinstance(value, dict):
            raise ProtocolError(f"{cls}.{name}: expected object")
        return {_decode(key_tp, k, cls, name): _decode(val_tp, v, cls, name) for k, v in value.items()}
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return tp.from_json(value)
    raise ProtocolError(f"{cls}.{name}: unsupported type {tp!r}")


def _encode(value: Any) -> Any:
    if isinstance(value, Wire):
        return value.to_json()
    if isinstance(value, Mapping):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode(v) for v in value]
    return value


class Wire:
    @classmethod
    def from_json(cls, doc: Any):
        if isinstance(doc, (bytes, bytearray, str)):
            doc = loads(bytes(doc) if isinstance(doc, bytearray) else doc)
        if not isinstance(doc, Mapping):
            raise ProtocolError(f"{cls.__name__}: expected object")
        hints = _hints(cls)
        decoders = _FIELD_DECODERS.get(cls.__name__, {})
        values: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name not in doc:
                if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
                    continue
                raise ProtocolError(f"{cls.__name__}: missing field {f.name!r}")
            raw = doc[f.name]
            custom = decoders.get(f.name)
            values[f.name] = custom(raw) if custom else _decode(hints[f.name], raw, cls.__name__, f.name)
        try:
            return cls(**values)
        except ProtocolError:
            raise
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"{cls.__name__}: {exc}") from exc

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):             # type: ignore[arg-type]
            value = getattr(self, f.name)
            if value is None and (f.default is not dataclasses.MISSING):
                continue
            out[f.name] = _encode(value)
        return out


@dataclass(frozen=True)
class Current(Wire):
    revision: int
    dataset_schema_version: int
    api_schema_version: int
    minimum_revision: int
    manifest: str
    manifest_sha256: str
    published_at: str
    checked_at: str
    expires_at: str
    sig: str


@dataclass(frozen=True)
class RequiresSnapshot(Wire):
    requires_snapshot: Literal[True]
    snapshot_revision: int
    from_revision: int
    reason: Literal["below_minimum", "package_missing"]


@dataclass(frozen=True)
class SnapshotRef(Wire):
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class UpdateRef(Wire):
    from_revision: int
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class BasePackRef(Wire):
    path: str
    sha256: str
    bytes: int
    icon_count: int


@dataclass(frozen=True)
class DeltaPackRef(Wire):
    from_revision: int
    path: str
    sha256: str
    bytes: int
    icon_count: int


@dataclass(frozen=True)
class LineageRef(Wire):
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class IconPacks(Wire):
    base: BasePackRef
    deltas: tuple[DeltaPackRef, ...]


@dataclass(frozen=True)
class Artifacts(Wire):
    snapshot: SnapshotRef
    updates: tuple[UpdateRef, ...]
    icon_packs: IconPacks
    lineage: LineageRef


@dataclass(frozen=True)
class Freshness(Wire):
    source_last_updated: str
    dataset_generated_at: str


@dataclass(frozen=True)
class Runtime(Wire):
    python: str
    duckdb: str
    dbt_core: str
    dbt_duckdb: str


@dataclass(frozen=True)
class Build(Wire):
    build_commit: str
    transform_run_id: str
    crawl_run_id: str
    seed_sha256: str
    parser_version: int
    catalog_version: str
    catalog_commit: str
    policy_version: str
    policy_commit: str
    runtime: Runtime


@dataclass(frozen=True)
class ManifestIcons(Wire):
    count: int
    withdrawn: tuple[str, ...]


@dataclass(frozen=True)
class GateFinding(Wire):
    check: str
    scope: str
    level: Literal["warning", "blocker"]
    count: int
    message: str
    pct: float | None = None


@dataclass(frozen=True)
class Gate(Wire):
    level: Literal["pass", "warning", "blocker"]
    warnings: tuple[GateFinding, ...]


@dataclass(frozen=True)
class Manifest(Wire):
    revision: int
    dataset_schema_version: int
    api_schema_version: int
    minimum_revision: int
    snapshot_only: bool
    snapshot_only_reasons: tuple[str, ...]
    freshness: Freshness
    build: Build
    entity_counts: Mapping[str, int]
    artifacts: Artifacts
    icons: ManifestIcons
    gate: Gate

    def update_from(self, revision: int) -> UpdateRef | None:
        return next((u for u in self.artifacts.updates if u.from_revision == revision), None)

    def delta_pack(self, from_revision: int) -> DeltaPackRef | None:
        return next((d for d in self.artifacts.icon_packs.deltas if d.from_revision == from_revision), None)


def _decode_row(raw: Any, where: str) -> dict[str, Scalar]:
    if not isinstance(raw, dict) or not raw:
        raise ProtocolError(f"{where}: a row must be a non-empty object")
    for column, value in raw.items():
        if not _ROW_COLUMN_RE.fullmatch(column):
            raise ProtocolError(f"{where}: bad column name {column[:40]!r}")
        if isinstance(value, bool) or not (value is None or isinstance(value, (str, int, float))):
            raise ProtocolError(f"{where}.{column}: row values must be string, integer, number or null")
    return dict(raw)


def _decode_data(raw: Any) -> dict[str, Any]:
    """table -> one row (object), a tuple of rows (array) or None (no row for a 1:1 subtype)"""
    if not isinstance(raw, dict):
        raise ProtocolError("UpsertOp.data: expected object")
    out: dict[str, Any] = {}
    for table, payload in raw.items():
        where = f"UpsertOp.data[{table!r}]"
        if payload is None:
            out[table] = None
        elif isinstance(payload, list):
            out[table] = tuple(_decode_row(r, where) for r in payload)
        else:
            out[table] = _decode_row(payload, where)
    return out


def _check_key(entity: str, key: str, where: str) -> None:
    if entity not in ENTITY_KEY_PATTERN:
        raise ProtocolError(f"{where}: unknown entity {entity!r}")
    if not isinstance(key, str) or not re.fullmatch(KEY_PATTERNS[ENTITY_KEY_PATTERN[entity]], key):
        raise ProtocolError(f"{where}: key {str(key)[:40]!r} does not fit entity {entity}")


@dataclass(frozen=True)
class UpsertOp(Wire):
    entity: str
    key: str
    rev: int
    data: Mapping[str, Row | tuple[Row, ...] | None]
    op: Literal["UPSERT"] = "UPSERT"

    def __post_init__(self) -> None:
        _check_key(self.entity, self.key, "UpsertOp")


@dataclass(frozen=True)
class RetireOp(Wire):
    entity: str
    key: str
    rev: int
    reason: str
    op: Literal["RETIRE"] = "RETIRE"

    def __post_init__(self) -> None:
        if self.entity != "equipment":
            raise ProtocolError("RetireOp: only equipment can be retired")
        _check_key(self.entity, self.key, "RetireOp")
        if self.reason not in RETIRE_REASONS:
            raise ProtocolError(f"RetireOp: reason {self.reason!r} not in {RETIRE_REASONS}")


@dataclass(frozen=True)
class AliasOp(Wire):
    entity: str
    key: str
    to: str
    rev: int
    confirmed_commit: str
    op: Literal["ALIAS"] = "ALIAS"

    def __post_init__(self) -> None:
        if self.entity != "equipment":
            raise ProtocolError("AliasOp: only equipment can be aliased")
        _check_key(self.entity, self.key, "AliasOp")
        _check_key(self.entity, self.to, "AliasOp.to")


@dataclass(frozen=True)
class DeleteOp(Wire):
    entity: str
    key: str
    rev: int
    op: Literal["DELETE"] = "DELETE"

    def __post_init__(self) -> None:
        if self.entity not in DELETABLE_ENTITIES:
            raise ProtocolError(f"DeleteOp: {self.entity!r} is not deletable")
        _check_key(self.entity, self.key, "DeleteOp")


Op = UpsertOp | RetireOp | AliasOp | DeleteOp
OP_TYPES: Mapping[str, type] = {"UPSERT": UpsertOp, "RETIRE": RetireOp, "ALIAS": AliasOp, "DELETE": DeleteOp}


def _decode_ops(raw: Any) -> tuple[Op, ...]:
    if not isinstance(raw, list):
        raise ProtocolError("UpdatePackage.ops: expected array")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ProtocolError("UpdatePackage.ops: every op must be an object")
        cls = OP_TYPES.get(item.get("op"))
        if cls is None:
            raise ProtocolError(f"UpdatePackage.ops: unknown op {str(item.get('op'))[:20]!r}")
        out.append(cls.from_json(item))
    return tuple(out)


@dataclass(frozen=True)
class IconDelta(Wire):
    pack: str | None
    pack_sha256: str | None
    added: tuple[str, ...]

    def __post_init__(self) -> None:
        if bool(self.added) != (self.pack is not None) or (self.pack is None) != (self.pack_sha256 is None):
            raise ProtocolError("IconDelta: pack and pack_sha256 are null if and only if added is empty")


@dataclass(frozen=True)
class UpdatePackage(Wire):
    from_revision: int
    to_revision: int
    dataset_schema_version: int
    ops: tuple[Op, ...]
    icons: IconDelta


@dataclass(frozen=True)
class IconPackEntry(Wire):
    sha256: str
    kind: Literal["equipment", "item", "ability"]
    bytes: int
    path: str


@dataclass(frozen=True)
class IconPackListing(Wire):
    name: str
    pack_type: Literal["base", "delta"]
    from_revision: int | None
    to_revision: int
    icons: tuple[IconPackEntry, ...]


@dataclass(frozen=True)
class ApiError(Wire):
    error: str
    message: str


_FIELD_DECODERS: dict[str, dict[str, Any]] = {
    "UpsertOp": {"data": _decode_data},
    "UpdatePackage": {"ops": _decode_ops},
}
