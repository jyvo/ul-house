import base64
import json
import re

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from ul_house.build import sign
from ul_house.build.sign import (
    ENV_PRIVATE_KEY,
    ENV_PUBLIC_KEYS,
    SignatureError,
    Signer,
    SigningKeyError,
    generate_keypair,
    load_public_key,
    trusted_public_keys,
    verify,
)

CURRENT = {
    "revision": 1847,
    "dataset_schema_version": 1,
    "api_schema_version": 1,
    "minimum_revision": 1817,
    "manifest": "manifests/1847.json",
    "manifest_sha256": "50d858e0985ecc7f60418aaf0cc5ab587f42c2570a884095a9e8ccacd0f6545c",
    "published_at": "2026-10-05T23:21:04Z",
    "checked_at": "2026-10-05T23:21:04Z",
    "expires_at": "2026-10-19T23:21:04Z",
}


@pytest.fixture
def pair():
    pem, public_hex = generate_keypair()
    return Signer.from_pem(pem), pem, public_hex


def test_sign_then_verify_returns_the_key_index(pair):
    signer, _, public_hex = pair
    signed = signer.sign(CURRENT)
    assert verify(signed, [public_hex]) == 0
    assert signer.public_key_hex() == public_hex


def test_signature_is_standard_padded_base64_of_64_bytes(pair):
    signer, _, _ = pair
    sig = signer.sign(CURRENT)["sig"]
    assert re.fullmatch(r"[A-Za-z0-9+/]{86}==", sig)
    assert len(base64.b64decode(sig, validate=True)) == 64


def test_signing_is_deterministic(pair):
    signer, _, _ = pair
    assert signer.sign(CURRENT) == signer.sign(CURRENT)


def test_sign_returns_a_copy_and_keeps_the_fields(pair):
    signer, _, _ = pair
    signed = signer.sign(CURRENT)
    assert "sig" not in CURRENT
    assert {k: v for k, v in signed.items() if k != "sig"} == CURRENT


def test_canonical_bytes_is_the_sync_implementation():
    from ul_house.sync import verify as sync_verify
    assert sign.canonical_bytes is sync_verify.canonical_bytes


def test_canonical_bytes_shape():
    a = {"b": 1, "a": "é", "sig": "ignored"}
    b = {"a": "é", "b": 1}
    assert sign.canonical_bytes(a) == sign.canonical_bytes(b) == b'{"a":"\\u00e9","b":1}'
    assert sign.canonical_bytes(a).isascii()
    assert b" " not in sign.canonical_bytes(CURRENT)


def test_canonical_bytes_matches_json_dumps_without_sig():
    expected = json.dumps(CURRENT, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert sign.canonical_bytes({**CURRENT, "sig": "x"}) == expected


def test_signature_does_not_depend_on_key_order(pair):
    signer, _, _ = pair
    reordered = dict(reversed(list(CURRENT.items())))
    assert signer.sign(reordered)["sig"] == signer.sign(CURRENT)["sig"]


def test_rotation_second_key_verifies_with_index_1(pair):
    old_signer, _, old_hex = pair
    new_pem, new_hex = generate_keypair()
    new_signer = Signer.from_pem(new_pem)
    assert verify(new_signer.sign(CURRENT), [old_hex, new_hex]) == 1
    assert verify(old_signer.sign(CURRENT), [old_hex, new_hex]) == 0


def test_unknown_key_is_refused(pair):
    _, _, public_hex = pair
    third = Signer.from_pem(generate_keypair()[0])
    with pytest.raises(SignatureError):
        verify(third.sign(CURRENT), [public_hex, generate_keypair()[1]])


def test_empty_key_list_is_refused(pair):
    signer, _, _ = pair
    with pytest.raises(SignatureError):
        verify(signer.sign(CURRENT), [])


@pytest.mark.parametrize("field, value", [
    ("revision", 1848),
    ("api_schema_version", 2),
    ("manifest_sha256", "0" * 64),
    ("expires_at", "2026-10-19T23:21:05Z"),
    ("minimum_revision", 1),
    ("manifest", "manifests/1848.json"),
])
def test_tampering_with_any_signed_field_is_detected(pair, field, value):
    signer, _, public_hex = pair
    signed = signer.sign(CURRENT)
    signed[field] = value
    with pytest.raises(SignatureError):
        verify(signed, [public_hex])


def test_an_added_field_breaks_the_signature(pair):
    signer, _, public_hex = pair
    signed = signer.sign(CURRENT)
    signed["extra"] = 1
    with pytest.raises(SignatureError):
        verify(signed, [public_hex])


def test_one_flipped_signature_bit_is_detected(pair):
    signer, _, public_hex = pair
    signed = signer.sign(CURRENT)
    raw = bytearray(base64.b64decode(signed["sig"]))
    raw[10] ^= 0x01
    signed["sig"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(SignatureError):
        verify(signed, [public_hex])


def test_missing_or_malformed_signature_is_refused(pair):
    signer, _, public_hex = pair
    with pytest.raises(SignatureError):
        verify(dict(CURRENT), [public_hex])
    for bad in ("not base64 !!", "AAAA", 5, None):
        with pytest.raises(SignatureError):
            verify({**CURRENT, "sig": bad}, [public_hex])


def test_resigning_replaces_an_existing_signature(pair):
    signer, _, public_hex = pair
    first = signer.sign(CURRENT)
    changed = {**first, "checked_at": "2026-10-06T23:21:04Z"}
    second = signer.sign(changed)
    assert second["sig"] != first["sig"]
    assert verify(second, [public_hex]) == 0
    with pytest.raises(SignatureError):
        verify(changed, [public_hex])


def test_from_env_loads_a_valid_pem(pair):
    _, pem, public_hex = pair
    signer = Signer.from_env({ENV_PRIVATE_KEY: pem.decode()})
    assert signer.public_key_hex() == public_hex


def test_from_env_missing_variable_names_it_and_leaks_nothing():
    with pytest.raises(SigningKeyError) as exc:
        Signer.from_env({})
    assert ENV_PRIVATE_KEY in str(exc.value)
    with pytest.raises(SigningKeyError):
        Signer.from_env({ENV_PRIVATE_KEY: "   "})


def test_from_env_error_never_contains_key_material(pair):
    _, pem, _ = pair
    garbage = pem.decode().replace("PRIVATE KEY", "PRIVATE KEY", 1)[:-40] + "garbage"
    body = "".join(pem.decode().splitlines()[1:-1])
    with pytest.raises(SigningKeyError) as exc:
        Signer.from_env({ENV_PRIVATE_KEY: garbage})
    assert body[:20] not in str(exc.value)


@pytest.mark.parametrize("make", [
    lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
    lambda: ec.generate_private_key(ec.SECP256R1()),
])
def test_non_ed25519_keys_are_rejected(make):
    pem = make().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())
    with pytest.raises(SigningKeyError):
        Signer.from_pem(pem)


def test_encrypted_and_garbage_pems_are_rejected():
    key = ed25519.Ed25519PrivateKey.generate()
    encrypted = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.BestAvailableEncryption(b"pw"))
    with pytest.raises(SigningKeyError):
        Signer.from_pem(encrypted)
    for garbage in (b"", b"not a pem", b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"):
        with pytest.raises(SigningKeyError):
            Signer.from_pem(garbage)


@pytest.mark.parametrize("bad", ["", "zz" * 32, "ab" * 31, "ab" * 33, "AB" * 32, "not hex", 5])
def test_load_public_key_rejects_non_hex_and_wrong_length(bad):
    with pytest.raises(SigningKeyError):
        load_public_key(bad)


def test_generate_keypair_roundtrips():
    pem, public_hex = generate_keypair()
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert re.fullmatch(r"[0-9a-f]{64}", public_hex)
    signed = Signer.from_pem(pem).sign(CURRENT)
    assert verify(signed, [public_hex]) == 0
    assert load_public_key(public_hex) is not None
    assert generate_keypair()[1] != public_hex


def test_trusted_keys_prefer_the_apps_embedded_keys(monkeypatch, pair):
    from ul_house.sync import keys
    _, _, public_hex = pair
    monkeypatch.setattr(keys, "PUBLIC_KEYS", ("ab" * 32, "cd" * 32))
    monkeypatch.setenv(ENV_PUBLIC_KEYS, public_hex)
    assert trusted_public_keys("local") == ("ab" * 32, "cd" * 32)
    assert trusted_public_keys("r2") == ("ab" * 32, "cd" * 32)


def test_trusted_keys_empty_app_keys_local_store_uses_the_environment(monkeypatch):
    from ul_house.sync import keys
    monkeypatch.setattr(keys, "PUBLIC_KEYS", ())
    env = {ENV_PUBLIC_KEYS: f"{'ab' * 32}, {'cd' * 32}"}
    assert trusted_public_keys("local", env) == ("ab" * 32, "cd" * 32)


def test_trusted_keys_fail_closed(monkeypatch):
    from ul_house.sync import keys
    monkeypatch.setattr(keys, "PUBLIC_KEYS", ())
    with pytest.raises(SigningKeyError):
        trusted_public_keys("r2", {ENV_PUBLIC_KEYS: "ab" * 32})        # never for R2
    with pytest.raises(SigningKeyError):
        trusted_public_keys("r2", {})
    with pytest.raises(SigningKeyError):
        trusted_public_keys("local", {})


SCRATCH = "cd" * 32


@pytest.mark.parametrize("base", ["http://127.0.0.1:8000", "http://localhost:9", "http://[::1]:8000",
                                  "HTTP://LOCALHOST:1"])
def test_dev_keys_are_honoured_for_a_loopback_api_base(monkeypatch, base):
    from ul_house.sync import keys
    monkeypatch.setattr(keys, "PUBLIC_KEYS", ("ab" * 32,))
    env = {sign.ENV_DEV_PUBLIC_KEYS: SCRATCH}
    assert trusted_public_keys("local", env, api_base=base) == (SCRATCH,)


@pytest.mark.parametrize("base", ["https://api.example.invalid", "http://192.0.2.10:8000",
                                  "http://127.0.0.1.example.invalid", None])
def test_dev_keys_are_ignored_off_loopback(monkeypatch, base):
    from ul_house.sync import keys
    monkeypatch.setattr(keys, "PUBLIC_KEYS", ("ab" * 32,))
    env = {sign.ENV_DEV_PUBLIC_KEYS: SCRATCH}
    assert trusted_public_keys("local", env, api_base=base) == ("ab" * 32,)


def test_dev_keys_are_ignored_for_r2_even_on_loopback(monkeypatch):
    from ul_house.sync import keys
    monkeypatch.setattr(keys, "PUBLIC_KEYS", ("ab" * 32,))
    env = {sign.ENV_DEV_PUBLIC_KEYS: SCRATCH}
    assert trusted_public_keys("r2", env, api_base="http://127.0.0.1:8000") == ("ab" * 32,)


def test_malformed_dev_keys_are_refused_on_loopback():
    value = "not-a-key-zzz"
    with pytest.raises(SigningKeyError) as err:
        trusted_public_keys("local", {sign.ENV_DEV_PUBLIC_KEYS: value}, api_base="http://127.0.0.1:8000")
    assert value not in str(err.value)


def test_production_keys_unchanged_without_override():
    from ul_house.sync import keys
    env = {sign.ENV_DEV_PUBLIC_KEYS: SCRATCH}
    for kind in ("local", "r2"):
        assert trusted_public_keys(kind) == tuple(keys.PUBLIC_KEYS) != ()
        assert trusted_public_keys(kind, env) == tuple(keys.PUBLIC_KEYS)


def test_dev_key_rule_matches_the_app():
    from ul_house.app import config
    assert sign.ENV_DEV_PUBLIC_KEYS == config.DEV_KEYS_ENV
    assert sign.LOOPBACK_HOSTS == config.LOOPBACK_HOSTS


def test_the_shipped_public_keys_start_empty():
    """without author keys, releases are refused"""
    from ul_house.sync import keys
    assert isinstance(keys.PUBLIC_KEYS, tuple)


def test_a_current_signed_here_passes_syncs_verifier(pair):
    verify_module = pytest.importorskip("ul_house.sync.verify")
    if not hasattr(verify_module, "verify_current"):
        pytest.skip("sync.verify.verify_current not present")
    signer, _, public_hex = pair
    signed = signer.sign(CURRENT)
    raw = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
    keys = verify_module.load_public_keys([public_hex])
    current = verify_module.verify_current(raw, keys)
    assert current.revision == CURRENT["revision"]
    assert current.sig == signed["sig"]
    tampered = json.dumps({**signed, "revision": 1848}, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(Exception):
        verify_module.verify_current(tampered, keys)
