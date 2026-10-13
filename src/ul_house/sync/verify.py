"""Signature and integrity checks"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import zlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ul_house.database import entities
from ul_house.sync import keys as _keys
from ul_house.sync.errors import ProtocolError, Refused, VerificationError
from ul_house.sync.protocol import API_SCHEMA_VERSION, UTC_RE, Current, Manifest, loads

MAX_JSON_BYTES = 1 * 2**20                  # current.json, manifest, index.json
MAX_PACKAGE_BYTES = 64 * 2**20              # decompressed package
MAX_SNAPSHOT_BYTES = 512 * 2**20            # decompressed snapshot
MAX_ICON_BYTES = 4 * 2**20
MAX_PACK_MEMBERS = 100_000
_CHUNK = 64 * 1024


def canonical_bytes(doc: Mapping) -> bytes:
    body = {k: v for k, v in doc.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def load_public_keys(hex_keys: Iterable[str] | None = None) -> tuple[Ed25519PublicKey, ...]:
    source = _keys.PUBLIC_KEYS if hex_keys is None else hex_keys
    out = []
    for text in source:
        try:
            raw = bytes.fromhex(text)
            if len(raw) != 32:
                raise ValueError("a public key is 32 bytes")
            out.append(Ed25519PublicKey.from_public_bytes(raw))
        except ValueError as exc:
            raise VerificationError(f"bad public key: {exc}") from exc
    return tuple(out)


def verify_current(raw: bytes, keys: Sequence[Ed25519PublicKey]) -> Current:
    if len(raw) > MAX_JSON_BYTES:
        raise VerificationError("current.json is too large")
    doc = loads(raw)
    sig_text = doc.get("sig")
    if not isinstance(sig_text, str):
        raise Refused("bad_signature", "current.json carries no signature")
    try:
        sig = base64.b64decode(sig_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Refused("bad_signature", "signature is not base64") from exc
    if len(sig) != 64:
        raise Refused("bad_signature", "signature has the wrong length")
    if not keys:
        raise Refused("bad_signature", "no trusted release key")
    try:
        message = canonical_bytes(doc)
    except (ValueError, TypeError) as exc:
        raise Refused("bad_signature", f"document cannot be canonicalised: {exc}") from exc
    for key in keys:
        try:
            key.verify(sig, message)
        except InvalidSignature:
            continue
        break
    else:
        raise Refused("bad_signature", "signature matches no trusted release key")
    return Current.from_json(doc)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_sha256(data: bytes, expected: str, what: str) -> None:
    actual = sha256_hex(data)
    if actual != expected:
        raise VerificationError(f"{what}: sha256 {actual} does not match the signed {expected}")


def check_supported(current: Current | Manifest, *,
                    dataset_schema_version: int = entities.DATASET_SCHEMA_VERSION,
                    api_schema_version: int = API_SCHEMA_VERSION) -> None:
    if current.dataset_schema_version != dataset_schema_version:
        raise Refused("schema_unsupported",
                      f"dataset schema {current.dataset_schema_version} is not supported: update the app")
    if current.api_schema_version > api_schema_version:
        raise Refused("api_unsupported",
                      f"API schema {current.api_schema_version} is newer than this app understands: update the app")


def check_revision(current: Current, local: int | None) -> Literal["same", "newer"]:
    if local is None or current.revision > local:
        return "newer"
    if current.revision < local:
        raise Refused("revision_lower", f"release {current.revision} is older than the applied {local}")
    return "same"


def check_invariants(current: Current, manifest: Manifest) -> None:
    if current.manifest != f"manifests/{current.revision}.json":
        raise VerificationError("current.manifest does not name this revision's manifest (INV-1)")
    if current.minimum_revision > current.revision:
        raise VerificationError("minimum_revision exceeds revision (INV-2)")
    for name in ("revision", "minimum_revision", "dataset_schema_version", "api_schema_version"):
        if getattr(manifest, name) != getattr(current, name):
            raise VerificationError(f"manifest {name} {getattr(manifest, name)} != current.json {getattr(current, name)}")


def _utc(text: str) -> datetime:
    if not UTC_RE.fullmatch(text):
        raise ProtocolError(f"not an ISO-8601 UTC timestamp: {text[:40]!r}")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def is_stale(expires_at: str, now: datetime) -> bool:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now > _utc(expires_at)


def _gunzip_chunks(blob: bytes, limit: int):
    """yield decompressed chunks of one gzip member"""
    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
    total = 0
    pending = blob
    try:
        while pending:
            chunk = decomp.decompress(pending, _CHUNK)
            pending = decomp.unconsumed_tail
            total += len(chunk)
            if total > limit:
                raise VerificationError(f"decompressed data exceeds {limit} bytes")
            if chunk:
                yield chunk
            if decomp.eof:
                break
        else:
            tail = decomp.flush()
            total += len(tail)
            if total > limit:
                raise VerificationError(f"decompressed data exceeds {limit} bytes")
            if tail:
                yield tail
    except zlib.error as exc:
        raise VerificationError(f"corrupt gzip stream: {exc}") from exc
    if not decomp.eof:
        raise VerificationError("truncated gzip stream")
    if decomp.unused_data:
        raise VerificationError("trailing data after the gzip stream")


def gunzip_bounded(blob: bytes, limit: int) -> bytes:
    return b"".join(_gunzip_chunks(blob, limit))


def gunzip_to_file(blob: bytes, dest: Path, limit: int) -> None:
    dest = Path(dest)
    try:
        with open(dest, "wb") as out:
            for chunk in _gunzip_chunks(blob, limit):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        raise
