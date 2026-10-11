"""routing core of the sync api (stdlib only)"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

REV = r"[1-9][0-9]{0,14}"
FROM_REV = r"(?:0|[1-9][0-9]{0,14})"
KIND = r"(?:equipment|item|ability)"
SHA = r"[0-9a-f]{64}"
PACK = rf"(?:base-{REV}|delta-{REV}-{REV})\.zip"

IMMUTABLE = "public, max-age=31536000, immutable"
POINTER = "public, max-age=300"
NO_STORE = "no-store"
JSON, GZIP, ZIP, PNG = "application/json", "application/gzip", "application/zip", "image/png"
RETRY_AFTER_SECONDS = 300

MAX_REVISION = 999_999_999_999_999
POINTER_KEY = "pub/current.json"

ALLOWED_PUB_KEYS: tuple[re.Pattern[str], ...] = (
    re.compile(r"current\.json"),
    re.compile(rf"manifests/{REV}\.json"),
    re.compile(rf"snapshots/unison-{REV}\.sqlite\.gz"),
    re.compile(rf"updates/{REV}-{REV}\.json\.gz"),
    re.compile(rf"icons/{KIND}/{SHA}\.png"),
    re.compile(rf"icons/packs/{PACK}"),
    re.compile(rf"lineage/{REV}\.json\.gz"),
)

_REV_RE = re.compile(REV)
_FROM_REV_RE = re.compile(FROM_REV)
_KIND_RE = re.compile(KIND)
_SHA_RE = re.compile(SHA)
_PACK_RE = re.compile(PACK)

_ROUTES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/v1/version"), "version"),
    (re.compile(rf"/v1/manifests/(?P<revision>[^/]*)"), "manifest"),
    (re.compile(rf"/v1/updates/(?P<from_revision>[^/]*)"), "updates"),
    (re.compile(rf"/v1/snapshots/(?P<revision>[^/]*)"), "snapshot"),
    (re.compile(rf"/v1/icons/packs/(?P<name>[^/]*)"), "pack"),
    (re.compile(rf"/v1/icons/(?P<kind>[^/]*)/(?P<sha256>[^/]*)"), "icon"),
    (re.compile(rf"/v1/lineage/(?P<revision>[^/]*)"), "lineage"),
)


class NotAllowed(Exception):
    """A store key outside the allow-list was requested (a bug, never a client error)."""


class PointerError(Exception):
    """current.json could not be read as a pointer."""


def pub_key(relative: str) -> str:
    """'manifests/2.json' -> pub/manifests/2.json"""
    if isinstance(relative, str) and any(p.fullmatch(relative) for p in ALLOWED_PUB_KEYS):
        return "pub/" + relative
    raise NotAllowed(f"key not allowed: {relative!r}")


def is_allowed_key(key: str) -> bool:
    """key starts with 'pub/' and its remainder passes pub_key()."""
    if not isinstance(key, str) or not key.startswith("pub/"):
        return False
    try:
        pub_key(key[4:])
    except NotAllowed:
        return False
    return True


@dataclass(frozen=True, slots=True)
class ObjectPlan:
    key: str        # full store key (pub/...)
    content_type: str
    cache_control: str


@dataclass(frozen=True, slots=True)
class UpdatesPlan:
    from_revision: int


@dataclass(frozen=True, slots=True)
class ErrorPlan:
    status: int     # 404 | 405 | 500 | 503
    code: str       # Error.error enum
    message: str


Plan = ObjectPlan | UpdatesPlan | ErrorPlan

_NOT_FOUND = ErrorPlan(404, "not_found", "not found")
_METHOD_NOT_ALLOWED = ErrorPlan(405, "method_not_allowed", "only GET is supported")


def plan_version() -> ObjectPlan:
    return ObjectPlan(pub_key("current.json"), JSON, POINTER)


def plan_manifest(revision: str) -> ObjectPlan | ErrorPlan:
    if not _REV_RE.fullmatch(revision):
        return _NOT_FOUND
    return ObjectPlan(pub_key(f"manifests/{revision}.json"), JSON, IMMUTABLE)


def plan_updates(from_revision: str) -> UpdatesPlan | ErrorPlan:
    if not _FROM_REV_RE.fullmatch(from_revision):
        return _NOT_FOUND
    return UpdatesPlan(int(from_revision))


def plan_snapshot(revision: str) -> ObjectPlan | ErrorPlan:
    if not _REV_RE.fullmatch(revision):
        return _NOT_FOUND
    return ObjectPlan(pub_key(f"snapshots/unison-{revision}.sqlite.gz"), GZIP, IMMUTABLE)


def plan_icon(kind: str, sha256: str) -> ObjectPlan | ErrorPlan:
    if not _KIND_RE.fullmatch(kind) or not _SHA_RE.fullmatch(sha256):
        return _NOT_FOUND
    return ObjectPlan(pub_key(f"icons/{kind}/{sha256}.png"), PNG, IMMUTABLE)


def plan_pack(name: str) -> ObjectPlan | ErrorPlan:
    if not _PACK_RE.fullmatch(name):
        return _NOT_FOUND
    return ObjectPlan(pub_key(f"icons/packs/{name}"), ZIP, IMMUTABLE)


def plan_lineage(revision: str) -> ObjectPlan | ErrorPlan:
    if not _REV_RE.fullmatch(revision):
        return _NOT_FOUND
    return ObjectPlan(pub_key(f"lineage/{revision}.json.gz"), GZIP, IMMUTABLE)


def resolve(method: str, raw_path: str) -> Plan:
    """for plain handler: raw, still percent-encoded path -> plan
    """
    if method != "GET":
        return _METHOD_NOT_ALLOWED
    for pattern, name in _ROUTES:
        m = pattern.fullmatch(raw_path)
        if m is None:
            continue
        g = m.groupdict()
        if name == "version":
            return plan_version()
        if name == "manifest":
            return plan_manifest(g["revision"])
        if name == "updates":
            return plan_updates(g["from_revision"])
        if name == "snapshot":
            return plan_snapshot(g["revision"])
        if name == "pack":
            return plan_pack(g["name"])
        if name == "icon":
            return plan_icon(g["kind"], g["sha256"])
        return plan_lineage(g["revision"])
    return _NOT_FOUND


@dataclass(frozen=True, slots=True)
class Pointer:
    revision: int
    minimum_revision: int


def _pointer_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PointerError(f"{name} is not an integer")
    if not 1 <= value <= MAX_REVISION:
        raise PointerError(f"{name} out of range")
    return value


def parse_pointer(raw: bytes) -> Pointer:
    """json.loads + the two ints"""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError, TypeError, RecursionError) as exc:
        raise PointerError("current.json is not JSON") from exc
    if not isinstance(doc, dict):
        raise PointerError("current.json is not an object")
    if "revision" not in doc or "minimum_revision" not in doc:
        raise PointerError("current.json lacks revision or minimum_revision")
    revision = _pointer_int(doc["revision"], "revision")
    minimum = _pointer_int(doc["minimum_revision"], "minimum_revision")
    if minimum > revision:
        raise PointerError("minimum_revision exceeds revision")
    return Pointer(revision, minimum)


@dataclass(frozen=True, slots=True)
class NoUpdate: ...


@dataclass(frozen=True, slots=True)
class BelowMinimum: ...


@dataclass(frozen=True, slots=True)
class Package:
    key: str        # 'pub/updates/{from}-{n}.json.gz'


UpdatesDecision = NoUpdate | BelowMinimum | Package


def decide_updates(pointer: Pointer, from_revision: int) -> UpdatesDecision:
    if from_revision >= pointer.revision:
        return NoUpdate()
    if from_revision < pointer.minimum_revision:
        return BelowMinimum()
    return Package(pub_key(f"updates/{from_revision}-{pointer.revision}.json.gz"))


def _canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def requires_snapshot_body(pointer: Pointer, from_revision: int, reason: str) -> bytes:
    return _canonical(
        {
            "requires_snapshot": True,
            "snapshot_revision": pointer.revision,
            "from_revision": from_revision,
            "reason": reason,
        }
    )


def error_body(code: str, message: str) -> bytes:
    return _canonical({"error": code, "message": message[:200]})


def strong_etag(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest() + '"'


def _strip_weak(tag: str) -> str:
    return tag[2:] if tag.startswith("W/") else tag


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    header = if_none_match.strip()
    if header == "*":
        return True
    target = _strip_weak(etag.strip())
    return any(_strip_weak(part.strip()) == target for part in header.split(",") if part.strip())
