from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

CHUNK = 1 << 20                     # 1 MiB streaming reads


class StoreError(Exception):
    pass


class ObjectMissing(StoreError, KeyError):
    """Absent key requested."""

    def __str__(self) -> str:       # avoid KeyError repr
        return Exception.__str__(self)


class IntegrityError(StoreError):
    """Read-back sha256 mismatch."""


class ImmutableConflict(StoreError):
    """Immutable put conflicts with existing."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    sha256: str | None              # None without stored metadata


class ObjectStore(Protocol):
    kind: str                                  # "local" | "r2"

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> ObjectInfo: ...
    def put_file(self, key: str, path: Path, *, content_type: str | None = None) -> ObjectInfo: ...
    def get(self, key: str, *, sha256: str | None = None) -> bytes: ...
    def get_file(self, key: str, dest: Path, *, sha256: str | None = None) -> ObjectInfo: ...
    def head(self, key: str) -> ObjectInfo | None: ...
    def list(self, prefix: str = "") -> list[ObjectInfo]: ...
    def delete(self, key: str) -> None: ...


def check_key(key: str) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError("object key must be a non-empty string")
    if key.startswith("/") or "\\" in key or "\x00" in key:
        raise ValueError(f"bad object key: {key!r}")
    segments = key.split("/")
    # list() tolerates trailing slash
    body = segments[:-1] if segments[-1] == "" and len(segments) > 1 else segments
    if any(seg in ("", ".", "..") for seg in body):
        raise ValueError(f"bad object key: {key!r}")
    return key


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _check_hash(key: str, expected: str | None, actual: str) -> None:
    if expected is not None and expected != actual:
        raise IntegrityError(f"{key}: sha256 {actual} != expected {expected}")


class Layout:
    SEED_CURRENT = "build/seed/current.sqlite"
    HISTORY_CURRENT = "build/history/current.duckdb"
    HISTORY_ARCHIVED = "build/history/archived/"
    CHECKPOINT = "build/history/checkpoint.json"
    CURRENT = "pub/current.json"
    PUB = "pub/"
    CANDIDATES = "candidates/"
    BACKUPS = "build/history/backups/"
    SEEDS = "build/seed/"
    HISTORIES = "build/history/"

    @staticmethod
    def seed_at(n: int) -> str:
        return f"build/seed/{n}.sqlite"

    @staticmethod
    def history_at(n: int) -> str:
        return f"build/history/{n}.duckdb"

    @staticmethod
    def history_backup(cid: str) -> str:
        return f"build/history/backups/pre-{cid}.duckdb"

    @staticmethod
    def archive(name: str) -> str:
        return f"build/history/archived/{name}.duckdb"

    @staticmethod
    def candidate(n: int) -> str:
        return f"candidates/{n}/"

    @staticmethod
    def rel_manifest(n: int) -> str:
        return f"manifests/{n}.json"

    @staticmethod
    def rel_snapshot(n: int) -> str:
        return f"snapshots/unison-{n}.sqlite.gz"

    @staticmethod
    def rel_update(r: int, n: int) -> str:
        return f"updates/{r}-{n}.json.gz"

    @staticmethod
    def rel_icon(kind: str, sha: str) -> str:
        return f"icons/{kind}/{sha}.png"

    @staticmethod
    def rel_base_pack(n: int) -> str:
        return f"icons/packs/base-{n}.zip"

    @staticmethod
    def rel_delta_pack(r: int, n: int) -> str:
        return f"icons/packs/delta-{r}-{n}.zip"

    @staticmethod
    def rel_lineage(n: int) -> str:
        return f"lineage/{n}.json.gz"

    @staticmethod
    def pub(rel: str) -> str:
        return "pub/" + rel

    # retention key parsers
    _MANIFEST = re.compile(r"^pub/manifests/([1-9][0-9]*)\.json$")
    _SNAPSHOT = re.compile(r"^pub/snapshots/unison-([1-9][0-9]*)\.sqlite\.gz$")
    _UPDATE = re.compile(r"^pub/updates/([1-9][0-9]*)-([1-9][0-9]*)\.json\.gz$")
    _ICON = re.compile(r"^pub/icons/(equipment|item|ability)/([0-9a-f]{64})\.png$")
    _BASE = re.compile(r"^pub/icons/packs/base-([1-9][0-9]*)\.zip$")
    _DELTA = re.compile(r"^pub/icons/packs/delta-([1-9][0-9]*)-([1-9][0-9]*)\.zip$")
    _LINEAGE = re.compile(r"^pub/lineage/([1-9][0-9]*)\.json\.gz$")
    _SEED = re.compile(r"^build/seed/([1-9][0-9]*)\.sqlite$")
    _HISTORY = re.compile(r"^build/history/([1-9][0-9]*)\.duckdb$")
    _BACKUP = re.compile(r"^build/history/backups/pre-([0-9A-Za-z._-]+)\.duckdb$")
    _CANDIDATE = re.compile(r"^candidates/([1-9][0-9]*)/(.+)$")

    @classmethod
    def parse_manifest_key(cls, key: str) -> int | None:
        m = cls._MANIFEST.match(key)
        return int(m.group(1)) if m else None

    @classmethod
    def parse_snapshot_key(cls, key: str) -> int | None:
        m = cls._SNAPSHOT.match(key)
        return int(m.group(1)) if m else None

    @classmethod
    def parse_update_key(cls, key: str) -> tuple[int, int] | None:
        m = cls._UPDATE.match(key)
        return (int(m.group(1)), int(m.group(2))) if m else None

    @classmethod
    def parse_icon_key(cls, key: str) -> tuple[str, str] | None:
        m = cls._ICON.match(key)
        return (m.group(1), m.group(2)) if m else None

    @classmethod
    def parse_base_pack_key(cls, key: str) -> int | None:
        m = cls._BASE.match(key)
        return int(m.group(1)) if m else None

    @classmethod
    def parse_delta_pack_key(cls, key: str) -> tuple[int, int] | None:
        m = cls._DELTA.match(key)
        return (int(m.group(1)), int(m.group(2))) if m else None

    @classmethod
    def parse_lineage_key(cls, key: str) -> int | None:
        m = cls._LINEAGE.match(key)
        return int(m.group(1)) if m else None

    @classmethod
    def parse_seed_key(cls, key: str) -> int | None:
        m = cls._SEED.match(key)
        return int(m.group(1)) if m else None

    @classmethod
    def parse_history_key(cls, key: str) -> int | None:
        m = cls._HISTORY.match(key)
        return int(m.group(1)) if m else None

    @classmethod
    def parse_backup_key(cls, key: str) -> str | None:
        m = cls._BACKUP.match(key)
        return m.group(1) if m else None

    @classmethod
    def parse_candidate_key(cls, key: str) -> tuple[int, str] | None:
        m = cls._CANDIDATE.match(key)
        return (int(m.group(1)), m.group(2)) if m else None


class LocalStore:
    kind = "local"

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def pub_dir(self) -> Path:
        return self.root / "pub"

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def _tmp(self) -> Path:
        tmp = self.root / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp / uuid.uuid4().hex

    def _install(self, tmp: Path, key: str) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> ObjectInfo:
        self._path(key)
        tmp = self._tmp()
        try:
            tmp.write_bytes(data)
            self._install(tmp, key)
        finally:
            tmp.unlink(missing_ok=True)
        return ObjectInfo(key, len(data), sha256_bytes(data))

    def put_file(self, key: str, path: Path, *, content_type: str | None = None) -> ObjectInfo:
        self._path(key)
        tmp = self._tmp()
        h = hashlib.sha256()
        size = 0
        try:
            with open(path, "rb") as src, open(tmp, "wb") as out:
                while chunk := src.read(CHUNK):
                    h.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            self._install(tmp, key)
        finally:
            tmp.unlink(missing_ok=True)
        return ObjectInfo(key, size, h.hexdigest())

    def get(self, key: str, *, sha256: str | None = None) -> bytes:
        path = self._path(key)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ObjectMissing(key) from None
        if sha256 is not None:
            _check_hash(key, sha256, sha256_bytes(data))
        return data

    def get_file(self, key: str, dest: Path, *, sha256: str | None = None) -> ObjectInfo:
        path = self._path(key)
        if not path.is_file():
            raise ObjectMissing(key)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
        h = hashlib.sha256()
        size = 0
        try:
            with open(path, "rb") as src, open(tmp, "wb") as out:
                while chunk := src.read(CHUNK):
                    h.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            _check_hash(key, sha256, h.hexdigest())
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return ObjectInfo(key, size, h.hexdigest())

    def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return ObjectInfo(key, path.stat().st_size, sha256_file(path))

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        out = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root)
            if rel.parts[0] == ".tmp":
                continue
            key = rel.as_posix()
            if key.startswith(prefix):
                out.append(ObjectInfo(key, path.stat().st_size, sha256_file(path)))
        return sorted(out, key=lambda info: info.key)

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != self.root:       # prune emptied directories
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


# r2
_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}


def _is_missing(exc: BaseException) -> bool:
    """detect botocore ClientError"""
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return False
    error = response.get("Error")
    code = error.get("Code") if isinstance(error, Mapping) else None
    return str(code) in _MISSING_CODES


# s3 compatable
class R2Store:
    kind = "r2"

    def __init__(self, client: Any, bucket: str):
        self.client, self.bucket = client, bucket

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> R2Store:
        env = os.environ if environ is None else environ
        for name in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
            if not env.get(name):
                raise StoreError(f"{name} not set")
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=env["R2_ENDPOINT"],
            aws_access_key_id=env["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
        return cls(client, env.get("R2_BUCKET") or "ul-house")

    def _put(self, key: str, body: Any, sha: str | None, size: int, content_type: str | None) -> ObjectInfo:
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Body": body}
        if content_type:
            kwargs["ContentType"] = content_type
        if sha is not None:
            kwargs["Metadata"] = {"sha256": sha}
        self.client.put_object(**kwargs)
        return ObjectInfo(key, size, sha)

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> ObjectInfo:
        check_key(key)
        return self._put(key, data, sha256_bytes(data), len(data), content_type)

    def put_file(self, key: str, path: Path, *, content_type: str | None = None) -> ObjectInfo:
        check_key(key)
        sha = sha256_file(path)                      # hash before PUT
        with open(path, "rb") as fh:
            return self._put(key, fh, sha, Path(path).stat().st_size, content_type)

    def _get(self, key: str) -> bytes:
        check_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _is_missing(exc):
                raise ObjectMissing(key) from None
            raise
        return response["Body"].read()

    def get(self, key: str, *, sha256: str | None = None) -> bytes:
        data = self._get(key)
        if sha256 is not None:
            _check_hash(key, sha256, sha256_bytes(data))
        return data

    def get_file(self, key: str, dest: Path, *, sha256: str | None = None) -> ObjectInfo:
        data = self._get(key)
        actual = sha256_bytes(data)
        _check_hash(key, sha256, actual)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return ObjectInfo(key, len(data), actual)

    def head(self, key: str) -> ObjectInfo | None:
        check_key(key)
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _is_missing(exc):
                return None
            raise
        metadata = response.get("Metadata") or {}
        sha = metadata.get("sha256") or metadata.get("Sha256")
        return ObjectInfo(key, int(response.get("ContentLength", 0)), sha)

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        out: list[ObjectInfo] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                out.append(ObjectInfo(item["Key"], int(item["Size"]), None))
            if response.get("IsTruncated"):
                token = response.get("NextContinuationToken")
                if not token:
                    break
            else:
                break
        return sorted(out, key=lambda info: info.key)

    def delete(self, key: str) -> None:
        check_key(key)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if not _is_missing(exc):
                raise


# helpers over any store
def put_immutable(store: ObjectStore, key: str, source: bytes | Path, *, content_type: str | None = None) -> tuple[ObjectInfo, bool]:
    """put object once; return info"""
    sha = sha256_bytes(source) if isinstance(source, (bytes, bytearray)) else sha256_file(Path(source))
    existing = store.head(key)
    if existing is not None:
        # read back to compare hash
        actual = existing.sha256 if existing.sha256 is not None else sha256_bytes(store.get(key))
        if actual != sha:
            raise ImmutableConflict(f"{key} already exists with sha256 {actual}, refusing {sha}")
        return ObjectInfo(key, existing.size, actual), False
    if isinstance(source, (bytes, bytearray)):
        return store.put(key, bytes(source), content_type=content_type), True
    return store.put_file(key, Path(source), content_type=content_type), True


def verify_object(store: ObjectStore, key: str, sha256: str) -> None:
    store.get(key, sha256=sha256)


def open_store(uri: str, environ: Mapping[str, str] | None = None) -> ObjectStore:
    if uri.startswith("local:") and len(uri) > len("local:"):
        return LocalStore(Path(uri[len("local:"):]))
    if uri == "r2":
        return R2Store.from_env(environ)
    raise ValueError(f"unknown store {uri!r}: use 'local:PATH' or 'r2'")


__all__ = [
    "CHUNK", "ImmutableConflict", "IntegrityError", "Layout", "LocalStore", "ObjectInfo", "ObjectMissing",
    "ObjectStore", "R2Store", "StoreError", "check_key", "open_store", "put_immutable", "sha256_bytes",
    "sha256_file", "verify_object",
]
