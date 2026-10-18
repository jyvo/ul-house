"""signatures, hashes, decompression, version, revision checks"""
import base64
import dataclasses
import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

import pub_fixture as pf
from ul_house.sync import keys, protocol, verify
from ul_house.sync.errors import ProtocolError, Refused, VerificationError


def signed(key=pf.KEY, **changes) -> bytes:
    return pf.encode_json(pf.sign_current(pf.sample_current_doc(**changes), key))


def trusted(*pairs):
    return verify.load_public_keys([p.public_hex for p in pairs])


def refused(raw, *pairs, reason="bad_signature"):
    with pytest.raises(Refused) as info:
        verify.verify_current(raw, trusted(*pairs))
    assert info.value.reason == reason


# signatures
def test_good_signature_verifies_and_returns_a_current():
    current = verify.verify_current(signed(), trusted(pf.KEY))
    assert isinstance(current, protocol.Current)
    assert current.revision == 4 and current.api_schema_version == 1 and current.manifest == "manifests/4.json"


def test_rotation_either_embedded_key_verifies():
    assert verify.verify_current(signed(pf.KEY2), trusted(pf.KEY, pf.KEY2)).revision == 4
    assert verify.verify_current(signed(pf.KEY), trusted(pf.KEY, pf.KEY2)).revision == 4


def test_a_signature_by_an_unknown_key_is_refused():
    refused(signed(pf.make_keypair("stranger")), pf.KEY, pf.KEY2)


def test_no_keys_fails_closed(monkeypatch):
    monkeypatch.setattr(keys, "PUBLIC_KEYS", ())
    assert verify.load_public_keys() == ()
    refused(signed(), )
    with pytest.raises(Refused) as info:
        verify.verify_current(signed(), verify.load_public_keys(keys.PUBLIC_KEYS))
    assert info.value.reason == "bad_signature"


@pytest.mark.parametrize("field,value", [
    ("revision", 5), ("dataset_schema_version", 2), ("api_schema_version", 2), ("minimum_revision", 2),
    ("manifest", "manifests/5.json"), ("manifest_sha256", "cd" * 32), ("published_at", "2026-10-02T00:00:00Z"),
    ("checked_at", "2026-10-05T00:00:00Z"), ("expires_at", "2030-01-01T00:00:00Z"),
])
def test_one_changed_field_breaks_the_signature(field, value):
    doc = json.loads(signed())
    doc[field] = value
    refused(pf.encode_json(doc), pf.KEY)


def test_a_flipped_byte_anywhere_is_refused():
    raw = bytearray(signed())
    index = raw.index(b"manifests/4") + 10
    raw[index] = ord("5")
    refused(bytes(raw), pf.KEY)


def test_equal_document_in_another_layout_still_verifies():
    doc = json.loads(signed())
    reordered = dict(reversed(list(doc.items())))
    for raw in (json.dumps(reordered).encode(), json.dumps(doc, indent=4, sort_keys=True).encode(),
                json.dumps(doc, separators=(",", ":")).encode()):
        assert verify.verify_current(raw, trusted(pf.KEY)).revision == 4


def test_an_added_unknown_field_is_covered_by_the_signature():
    doc = json.loads(signed())
    doc["freeze_notice"] = "hello"
    refused(pf.encode_json(doc), pf.KEY)            # not re-signed: refused
    resigned = pf.sign_current(doc, pf.KEY)
    current = verify.verify_current(pf.encode_json(resigned), trusted(pf.KEY))
    assert current.revision == 4 and not hasattr(current, "freeze_notice")      # re-signed: accepted, field ignored


def test_the_signature_is_checked_before_any_field_is_believed():
    doc = pf.sample_current_doc(revision="not a number")
    refused(pf.encode_json(pf.sign_current(doc, pf.make_keypair("stranger"))), pf.KEY)


@pytest.mark.parametrize("sig", [
    "not base64 !!!", "", base64.b64encode(b"short").decode(), base64.b64encode(bytes(65)).decode(), 12345, None,
])
def test_a_malformed_signature_is_refused(sig):
    doc = pf.sample_current_doc()
    doc["sig"] = sig
    refused(pf.encode_json(doc), pf.KEY)


def test_a_missing_signature_is_refused():
    refused(pf.encode_json(pf.sample_current_doc()), pf.KEY)


def test_not_json_is_a_protocol_error():
    for raw in (b"not json", b"[]", b'{"a": 1, "a": 2}', b'{"revision": NaN}', b""):
        with pytest.raises(ProtocolError):
            verify.verify_current(raw, trusted(pf.KEY))


def test_oversize_current_json_is_refused_before_parsing():
    raw = signed() + b" " * verify.MAX_JSON_BYTES
    with pytest.raises(VerificationError):
        verify.verify_current(raw, trusted(pf.KEY))


def test_bad_public_keys_are_rejected():
    for bad in ("zz", "ab" * 31, "ab" * 33, ""):
        with pytest.raises(VerificationError):
            verify.load_public_keys([bad])


def test_canonical_form_is_json_dumps_with_ascii_escapes_byte_for_byte():
    doc = {"b": "café ☃", "a": [1, 2, {"z": None, "y": True}], "sig": "ignored", "c": 1.5}
    expected = json.dumps({"b": "café ☃", "a": [1, 2, {"z": None, "y": True}], "c": 1.5}, sort_keys=True,
                          separators=(",", ":"), allow_nan=False, ensure_ascii=True).encode("utf-8")
    assert verify.canonical_bytes(doc) == expected
    assert b"\\u00e9" in expected and b"\\u2603" in expected and all(b < 0x80 for b in expected)
    assert verify.canonical_bytes(doc) == verify.canonical_bytes({k: v for k, v in doc.items() if k != "sig"})
    assert b" " not in verify.canonical_bytes({"a": 1, "b": 2})
    assert verify.canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    with pytest.raises(ValueError):
        verify.canonical_bytes({"a": float("nan")})


def test_a_non_ascii_document_signs_and_verifies():
    doc = pf.sample_current_doc(note="café")
    raw = pf.encode_json(pf.sign_current(doc, pf.KEY))
    assert verify.verify_current(raw, trusted(pf.KEY)).revision == 4


def test_t4_signature_description_agrees_with_canonical_bytes():
    """api/ cannot import ul_house; must agree."""
    doc = pf.sample_current_doc()
    reimplemented = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert verify.canonical_bytes({**doc, "sig": "x"}) == reimplemented


# sha256 + bounded decomp
def test_require_sha256():
    data = b"payload"
    verify.require_sha256(data, verify.sha256_hex(data), "x")
    assert verify.sha256_hex(data) == pf.sha256_hex(data) and len(verify.sha256_hex(data)) == 64
    with pytest.raises(VerificationError):
        verify.require_sha256(data, "0" * 64, "x")
    with pytest.raises(VerificationError):
        verify.require_sha256(data, verify.sha256_hex(data).upper(), "x")


def test_gunzip_bounded():
    payload = b"x" * 10_000
    assert verify.gunzip_bounded(gzip.compress(payload), 10_000) == payload
    with pytest.raises(VerificationError):
        verify.gunzip_bounded(gzip.compress(payload), 9_999)                   # over limit
    with pytest.raises(VerificationError):
        verify.gunzip_bounded(b"definitely not gzip", 100)
    with pytest.raises(VerificationError):
        verify.gunzip_bounded(gzip.compress(payload)[:-10], 100_000)           # truncated stream
    bomb = gzip.compress(b"\x00" * (50 * 2**20))                               # tiny blob, huge output
    assert len(bomb) < 100_000
    with pytest.raises(VerificationError):
        verify.gunzip_bounded(bomb, 1 * 2**20)


def test_gunzip_to_file(tmp_path):
    payload = b"snapshot bytes" * 1000
    dest = tmp_path / "out.sqlite"
    verify.gunzip_to_file(gzip.compress(payload), dest, len(payload))
    assert dest.read_bytes() == payload
    with pytest.raises(VerificationError):
        verify.gunzip_to_file(gzip.compress(payload), tmp_path / "small.sqlite", 10)
    with pytest.raises(VerificationError):
        verify.gunzip_to_file(b"junk", tmp_path / "junk.sqlite", 100)


def test_limits_are_the_documented_ones():
    assert verify.MAX_JSON_BYTES == 2**20 and verify.MAX_PACKAGE_BYTES == 64 * 2**20
    assert verify.MAX_SNAPSHOT_BYTES == 512 * 2**20 and verify.MAX_ICON_BYTES == 4 * 2**20
    assert verify.MAX_PACK_MEMBERS == 100_000


# revisions, versions, staleness, invariants
def current_of(**changes) -> protocol.Current:
    return protocol.Current.from_json(pf.sample_current_doc(sig="A" * 86 + "==", **changes))


def test_check_revision():
    current = current_of(revision=5)
    assert verify.check_revision(current, None) == "newer"
    assert verify.check_revision(current, 4) == "newer"
    assert verify.check_revision(current, 5) == "same"
    with pytest.raises(Refused) as info:
        verify.check_revision(current, 6)
    assert info.value.reason == "revision_lower"


def test_check_supported():
    verify.check_supported(current_of())
    for dataset in (2, 99):
        with pytest.raises(Refused) as info:
            verify.check_supported(current_of(dataset_schema_version=dataset))
        assert info.value.reason == "schema_unsupported"
    with pytest.raises(Refused) as info:
        verify.check_supported(current_of(api_schema_version=2))
    assert info.value.reason == "api_unsupported"
    verify.check_supported(current_of(api_schema_version=protocol.API_SCHEMA_VERSION))


def test_is_stale_boundary():
    expires = "2026-10-18T00:00:00Z"
    at = datetime(2026, 10, 18, tzinfo=timezone.utc)
    assert verify.is_stale(expires, at) is False
    assert verify.is_stale(expires, at + timedelta(seconds=1)) is True
    assert verify.is_stale(expires, at - timedelta(days=1)) is False


def manifest_of(pub_dir, **changes) -> protocol.Manifest:
    doc = json.loads((pub_dir / "manifests" / "4.json").read_text())
    doc.update(changes)
    return protocol.Manifest.from_json(doc)


@pytest.fixture(scope="module")
def pub(tmp_path_factory):
    return pf.build_pub(tmp_path_factory.mktemp("verify_pub"))


def test_invariants_hold_for_a_good_release(pub):
    verify.check_invariants(current_of(), manifest_of(pub.dir))


@pytest.mark.parametrize("current_changes,manifest_changes", [
    ({"manifest": "manifests/3.json"}, {}),
    ({"minimum_revision": 5}, {"minimum_revision": 5}),
    ({}, {"revision": 3}),
    ({}, {"minimum_revision": 2}),
    ({}, {"dataset_schema_version": 2}),
    ({}, {"api_schema_version": 2}),
])
def test_invariant_violations_are_refused(pub, current_changes, manifest_changes):
    with pytest.raises(VerificationError):
        verify.check_invariants(current_of(**current_changes), manifest_of(pub.dir, **manifest_changes))


def test_current_is_a_frozen_value():
    current = current_of()
    with pytest.raises(dataclasses.FrozenInstanceError):
        current.revision = 9
