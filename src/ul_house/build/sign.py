from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ul_house.sync import keys as _keys
from ul_house.sync.keys import PUBLIC_KEYS
from ul_house.sync.verify import canonical_bytes

ENV_PRIVATE_KEY = "RELEASE_SIGNING_KEY"
ENV_PUBLIC_KEYS = "RELEASE_PUBLIC_KEYS"
ENV_DEV_PUBLIC_KEYS = "UL_HOUSE_DEV_PUBLIC_KEYS"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IMPORTED_KEYS = PUBLIC_KEYS


class SigningKeyError(Exception):
    pass


class SignatureError(Exception):
    pass


def _raw_public_hex(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return raw.hex()


@dataclass(frozen=True)
class Signer:
    key: Ed25519PrivateKey

    @classmethod
    def from_pem(cls, pem: bytes | str) -> Signer:
        data = pem.encode("utf-8") if isinstance(pem, str) else pem
        try:
            key = serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError, binascii.Error):
            # never echo key
            raise SigningKeyError("signing key is not an unencrypted PEM private key") from None
        if not isinstance(key, Ed25519PrivateKey):
            raise SigningKeyError("signing key is not an Ed25519 private key")
        return cls(key)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, var: str = ENV_PRIVATE_KEY) -> Signer:
        env = os.environ if environ is None else environ
        value = env.get(var)
        if not value or not value.strip():
            raise SigningKeyError(f"{var} is not set")
        if "\\n" in value and "\n" not in value:            # literal backslash-n secret
            value = value.replace("\\n", "\n")
        return cls.from_pem(value)

    def public_key_hex(self) -> str:
        return _raw_public_hex(self.key)

    def sign(self, current: Mapping[str, Any]) -> dict[str, Any]:
        """Copy of `current` with `sig`."""
        body = {k: v for k, v in current.items() if k != "sig"}
        signature = self.key.sign(canonical_bytes(body))
        return {**body, "sig": base64.b64encode(signature).decode("ascii")}


def load_public_key(hex_key: str) -> Ed25519PublicKey:
    if not isinstance(hex_key, str) or not _HEX64.match(hex_key):
        raise SigningKeyError("a public key is 64 lowercase hex characters")
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_key))


def verify(current: Mapping[str, Any], public_keys: Sequence[str]) -> int:
    """idx of first verifying key"""
    sig_text = current.get("sig")
    if not isinstance(sig_text, str):
        raise SignatureError("current.json has no signature")
    try:
        signature = base64.b64decode(sig_text, validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError("signature is not base64") from None
    if len(signature) != 64:
        raise SignatureError("signature has the wrong length")
    if not public_keys:
        raise SignatureError("no public key to verify with")
    message = canonical_bytes(current)
    for index, hex_key in enumerate(public_keys):
        try:
            load_public_key(hex_key).verify(signature, message)
        except InvalidSignature:
            continue
        return index
    raise SignatureError("signature does not match any trusted key")


def _app_keys() -> tuple[str, ...]:
    """app's embedded keys"""
    if PUBLIC_KEYS is not _IMPORTED_KEYS:
        return tuple(PUBLIC_KEYS)
    return tuple(_keys.PUBLIC_KEYS)


def is_loopback(url: str | None) -> bool:
    """whether the URL targets loopback"""
    if not url:
        return False
    try:
        return (urlsplit(url).hostname or "").lower() in LOOPBACK_HOSTS
    except ValueError:
        return False


def dev_public_keys(store_kind: str, api_base: str | None,
                    environ: Mapping[str, str] | None = None) -> tuple[str, ...] | None:
    """dev keys, loopback local only"""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_DEV_PUBLIC_KEYS, "")
    if not raw.strip() or store_kind != "local" or not is_loopback(api_base):
        return None
    keys = tuple(k.strip().lower() for k in raw.split(",") if k.strip())
    if not keys or not all(_HEX64.match(k) for k in keys):
        raise SigningKeyError(f"{ENV_DEV_PUBLIC_KEYS}: every key must be 64 hex characters")
    return keys


def trusted_public_keys(store_kind: str, environ: Mapping[str, str] | None = None, *, api_base: str | None = None) -> tuple[str, ...]:
    """keys a release must verify under"""
    dev = dev_public_keys(store_kind, api_base, environ)
    if dev:
        return dev
    app = _app_keys()
    if app:
        return app
    if store_kind == "local":
        env = os.environ if environ is None else environ
        keys = tuple(k.strip() for k in env.get(ENV_PUBLIC_KEYS, "").split(",") if k.strip())
        if keys:
            return keys
        raise SigningKeyError(
            f"no trusted public key: sync.keys.PUBLIC_KEYS is empty and {ENV_PUBLIC_KEYS} is not set")
    raise SigningKeyError("sync.keys.PUBLIC_KEYS is empty: refusing to publish a release the app would not trust")


def generate_keypair() -> tuple[bytes, str]:
    """gen private PEM and public hex"""
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return pem, _raw_public_hex(key)


__all__ = [
    "ENV_DEV_PUBLIC_KEYS", "ENV_PRIVATE_KEY", "ENV_PUBLIC_KEYS", "LOOPBACK_HOSTS", "PUBLIC_KEYS",
    "SignatureError", "Signer", "SigningKeyError", "canonical_bytes", "dev_public_keys", "generate_keypair",
    "is_loopback", "load_public_key", "trusted_public_keys", "verify",
]
