from __future__ import annotations

import hashlib
import json

import pytest

from syncapi import routing
from syncapi.routing import (
    BelowMinimum,
    ErrorPlan,
    NoUpdate,
    ObjectPlan,
    Package,
    Pointer,
    PointerError,
    UpdatesPlan,
)

SHA = "a" * 64
VALID_REVISIONS = ["1", "2", "10", "123456789", "999999999999999"]
BAD_REVISIONS = ["0", "01", "-1", "1.0", "1e3", "１", "1\n", "1 ", " 1", "1000000000000000", "", "abc", "0x1", "+1", "١"]


def is_error(plan, status=404, code="not_found"):
    return isinstance(plan, ErrorPlan) and plan.status == status and plan.code == code


# param tables
@pytest.mark.parametrize("planner", [routing.plan_manifest, routing.plan_snapshot, routing.plan_lineage])
@pytest.mark.parametrize("value", VALID_REVISIONS)
def test_revision_accepted(planner, value):
    assert isinstance(planner(value), ObjectPlan)


@pytest.mark.parametrize("planner", [routing.plan_manifest, routing.plan_snapshot, routing.plan_lineage])
@pytest.mark.parametrize("value", BAD_REVISIONS)
def test_revision_rejected(planner, value):
    assert is_error(planner(value))


@pytest.mark.parametrize("value", ["0", *VALID_REVISIONS])
def test_from_revision_accepted(value):
    assert routing.plan_updates(value) == UpdatesPlan(int(value))


@pytest.mark.parametrize("value", [v for v in BAD_REVISIONS if v != "0"])
def test_from_revision_rejected(value):
    assert is_error(routing.plan_updates(value))


@pytest.mark.parametrize("kind", ["equipment", "item", "ability"])
def test_kind_accepted(kind):
    plan = routing.plan_icon(kind, SHA)
    assert plan == ObjectPlan(f"pub/icons/{kind}/{SHA}.png", "image/png", routing.IMMUTABLE)


@pytest.mark.parametrize("kind", ["packs", "Equip", "EQUIPMENT", "skill", "a/b", "", "equipment ", "equipment\n", "weapon"])
def test_kind_rejected(kind):
    assert is_error(routing.plan_icon(kind, SHA))


@pytest.mark.parametrize("sha", ["A" * 64, "a" * 63, "a" * 65, "g" * 64, "", "a" * 64 + "\n", "a" * 63 + "é", "0" * 64 + ".png"])
def test_sha256_rejected(sha):
    assert is_error(routing.plan_icon("equipment", sha))


@pytest.mark.parametrize("sha", ["0" * 64, "a" * 64, "0123456789abcdef" * 4])
def test_sha256_accepted(sha):
    assert isinstance(routing.plan_icon("equipment", sha), ObjectPlan)


@pytest.mark.parametrize("name", ["base-2.zip", "delta-1-2.zip", "base-999999999999999.zip", "delta-10-20.zip"])
def test_pack_names_accepted(name):
    assert routing.plan_pack(name) == ObjectPlan(f"pub/icons/packs/{name}", "application/zip", routing.IMMUTABLE)


@pytest.mark.parametrize("name", ["base-2", "delta-1.zip", "base-02.zip", "../x.zip", "base-0.zip", "delta-1-2-3.zip", "Base-2.zip", "base-2.zip\n", "delta-1-2", "", "base-.zip", "x/base-2.zip"])
def test_pack_names_rejected(name):
    assert is_error(routing.plan_pack(name))


def test_plan_fields_per_route():
    assert routing.plan_version() == ObjectPlan("pub/current.json", "application/json", "public, max-age=300")
    assert routing.plan_manifest("7") == ObjectPlan("pub/manifests/7.json", "application/json", "public, max-age=31536000, immutable")
    assert routing.plan_snapshot("7") == ObjectPlan("pub/snapshots/unison-7.sqlite.gz", "application/gzip", "public, max-age=31536000, immutable")
    assert routing.plan_lineage("7") == ObjectPlan("pub/lineage/7.json.gz", "application/gzip", "public, max-age=31536000, immutable")


def test_constants():
    assert routing.IMMUTABLE == "public, max-age=31536000, immutable"
    assert routing.POINTER == "public, max-age=300"
    assert routing.NO_STORE == "no-store"
    assert routing.RETRY_AFTER_SECONDS == 300


# allow list
def test_pub_key_accepts_every_fixture_object(store):
    assert len(store.objects) == 8
    for relative in store.objects:
        assert routing.pub_key(relative) == f"pub/{relative}"
        assert routing.is_allowed_key(f"pub/{relative}")


REJECTED_KEYS = [
    "", "/current.json", "../build/seed/current.sqlite", "build/seed/current.sqlite", "pub/current.json",
    "manifests/../current.json", "manifests\\2.json", "current.json\n", "current.json\x00", "%2e%2e/x",
    f"icons/packs/{SHA}.png", "CURRENT.JSON", "current.json/", "./current.json", "manifests/02.json",
    "manifests/0.json", "manifests/2.json.gz", "snapshots/unison-2.sqlite", "updates/1-.json.gz", "updates/1.json.gz",
    f"icons/skill/{SHA}.png", f"icons/equipment/{SHA}", f"icons/equipment/{SHA.upper()}.png", "lineage/2.json",
    "icons/packs/base-2", "candidates/3/manifests/3.json", "secret.txt", "build/history/checkpoint.json",
    "manifests/١.json", "manifests/2.json\n", " manifests/2.json",
]


@pytest.mark.parametrize("key", REJECTED_KEYS, ids=[repr(k) for k in REJECTED_KEYS])
def test_pub_key_rejects(key):
    with pytest.raises(routing.NotAllowed):
        routing.pub_key(key)


@pytest.mark.parametrize("key", [k for k in REJECTED_KEYS if k != "pub/current.json"] + ["", "pub/", "pub/../build/seed/current.sqlite", "pub//current.json", "PUB/current.json", "current.json", "/pub/current.json"])
def test_is_allowed_key_rejects_without_pub_prefix_or_pattern(key):
    assert routing.is_allowed_key(key) is False


@pytest.mark.parametrize("key", ["pub/current.json\n", "pub/current.json ", "pub/manifests/../current.json", "pub/build/seed/current.sqlite"])
def test_is_allowed_key_rejects_pub_prefixed_junk(key):
    assert routing.is_allowed_key(key) is False


# decide_updates
@pytest.mark.parametrize("from_revision", [0, 1, 2, 3, 4])
def test_decide_below_minimum(from_revision):
    assert routing.decide_updates(Pointer(10, 5), from_revision) == BelowMinimum()


@pytest.mark.parametrize("from_revision", [5, 6, 9])
def test_decide_package(from_revision):
    assert routing.decide_updates(Pointer(10, 5), from_revision) == Package(f"pub/updates/{from_revision}-10.json.gz")


@pytest.mark.parametrize("from_revision", [10, 11, 10**14])
def test_decide_no_update(from_revision):
    assert routing.decide_updates(Pointer(10, 5), from_revision) == NoUpdate()


def test_decide_snapshot_only_release():
    pointer = Pointer(10, 10)
    assert routing.decide_updates(pointer, 9) == BelowMinimum()
    assert routing.decide_updates(pointer, 0) == BelowMinimum()
    assert routing.decide_updates(pointer, 10) == NoUpdate()


def test_decide_package_keys_pass_the_allow_list():
    for from_revision in range(5, 10):
        assert routing.is_allowed_key(routing.decide_updates(Pointer(10, 5), from_revision).key)


# parse_pointer
def pointer_bytes(**fields):
    return json.dumps({"revision": 5, "minimum_revision": 2, **fields}).encode()


def test_parse_pointer_ok():
    assert routing.parse_pointer(pointer_bytes()) == Pointer(5, 2)
    assert routing.parse_pointer(pointer_bytes(revision=5, minimum_revision=5)) == Pointer(5, 5)


def test_parse_pointer_ignores_other_fields():
    assert routing.parse_pointer(pointer_bytes(sig="x", whatever=[1])) == Pointer(5, 2)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json", b"", b"\xff\xfe", b"[]", b"1", b"null", b'{"revision": 5}', b'{"minimum_revision": 2}',
        b'{"revision": "5", "minimum_revision": 2}', b'{"revision": true, "minimum_revision": 1}',
        b'{"revision": 5, "minimum_revision": false}', b'{"revision": 2, "minimum_revision": 5}',
        b'{"revision": 5.0, "minimum_revision": 2}', b'{"revision": 0, "minimum_revision": 0}',
        b'{"revision": 1000000000000000, "minimum_revision": 1}', b'{"revision": -5, "minimum_revision": 1}',
        b'{"revision": null, "minimum_revision": null}',
    ],
)
def test_parse_pointer_rejects(raw):
    with pytest.raises(PointerError):
        routing.parse_pointer(raw)


# etag_matches
ETAG = '"abc123"'


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (ETAG, True), ("*", True), (' * ', True), (f'"x", {ETAG}', True), (f'{ETAG},"x"', True), (f'W/{ETAG}', True),
        (f'"x", W/{ETAG} , "y"', True), ("abc123", False), ('"other"', False), ('"abc1234"', False), (None, False),
        ("", False), ('"x", "y"', False), ('"abc12"', False),
    ],
)
def test_etag_matches(header, expected):
    assert routing.etag_matches(header, ETAG) is expected


def test_etag_matches_with_a_weak_stored_etag():
    assert routing.etag_matches('"abc123"', 'W/"abc123"') is True


def test_strong_etag():
    assert routing.strong_etag(b"hello") == '"' + hashlib.sha256(b"hello").hexdigest() + '"'


# resolve tests
RESOLVE_OK = [
    ("/v1/version", ObjectPlan("pub/current.json", "application/json", "public, max-age=300")),
    ("/v1/manifests/2", ObjectPlan("pub/manifests/2.json", "application/json", routing.IMMUTABLE)),
    ("/v1/updates/1", UpdatesPlan(1)),
    ("/v1/updates/0", UpdatesPlan(0)),
    ("/v1/snapshots/2", ObjectPlan("pub/snapshots/unison-2.sqlite.gz", "application/gzip", routing.IMMUTABLE)),
    ("/v1/icons/packs/base-2.zip", ObjectPlan("pub/icons/packs/base-2.zip", "application/zip", routing.IMMUTABLE)),
    ("/v1/icons/packs/delta-1-2.zip", ObjectPlan("pub/icons/packs/delta-1-2.zip", "application/zip", routing.IMMUTABLE)),
    (f"/v1/icons/equipment/{SHA}", ObjectPlan(f"pub/icons/equipment/{SHA}.png", "image/png", routing.IMMUTABLE)),
    ("/v1/lineage/2", ObjectPlan("pub/lineage/2.json.gz", "application/gzip", routing.IMMUTABLE)),
]


@pytest.mark.parametrize(("path", "expected"), RESOLVE_OK, ids=[p for p, _ in RESOLVE_OK])
def test_resolve_contract_paths(path, expected):
    assert routing.resolve("GET", path) == expected


RESOLVE_404 = [
    "", "/", "/v1", "/v1/", "/v1/version/", "//v1/version", "/v1//version", "/v1/VERSION", "/V1/version", "v1/version",
    "/v2/version", "/v1/manifests", "/v1/manifests/", "/v1/manifests/2/", "/v1/manifests/%32", "/v1/manifests/2%20",
    "/v1/manifests/..%2F..%2Fbuild%2Fseed%2Fcurrent", "/v1/manifests/../version", "/v1/manifests/./2", "/v1/manifests/..",
    "/v1/icons/packs/../../x", "/v1/icons/packs/%2e%2e", "/v1/icons/%2e%2e/x/y", "/v1/icons/packs", "/v1/icons/packs/",
    f"/v1/icons/packs/{SHA}", "/v1/icons/equipment", f"/v1/icons/equipment/{SHA}/", f"/v1/icons/equipment/{SHA}.png",
    "/v1/updates/-1", "/v1/updates/01", "/v1/updates/1/2", "/v1/lineage/2.json.gz", "/v1/snapshots/unison-2.sqlite.gz",
    "/docs", "/openapi.json", "/redoc", "/pub/current.json", "/build/seed/current.sqlite", "/v1/version\n", "/v1/version\x00",
    "/v1/manifests/2\n", "/v1/manifests/2.json", "/v1/snapshots/%2e%2e%2f", "/v1/lineage/%2e%2e",
]


@pytest.mark.parametrize("path", RESOLVE_404, ids=[repr(p) for p in RESOLVE_404])
def test_resolve_unknown_paths_are_404(path):
    assert is_error(routing.resolve("GET", path))


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "get", "Get"])
@pytest.mark.parametrize("path", ["/v1/version", "/v1/updates/1", "/nothing/here"])
def test_resolve_non_get_is_405(method, path):
    assert is_error(routing.resolve(method, path), 405, "method_not_allowed")


def test_resolve_never_decodes():
    assert is_error(routing.resolve("GET", "/v1/manifests/%32"))
    assert is_error(routing.resolve("GET", "/v1/%76ersion"))


# bodies tests
def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@pytest.mark.parametrize("reason", ["below_minimum", "package_missing"])
@pytest.mark.parametrize("from_revision", [0, 3, 9])
def test_requires_snapshot_body_is_valid_and_canonical(contract, reason, from_revision):
    body = routing.requires_snapshot_body(Pointer(10, 5), from_revision, reason)
    document = json.loads(body)
    contract.validate("requires_snapshot", document)
    assert document == {"requires_snapshot": True, "snapshot_revision": 10, "from_revision": from_revision, "reason": reason}
    assert body == canonical(document)


@pytest.mark.parametrize("code", ["not_found", "no_update", "method_not_allowed", "unavailable", "internal"])
def test_error_body_is_valid_and_canonical(contract, code):
    body = routing.error_body(code, "something happened")
    document = json.loads(body)
    contract.validate_error(document)
    assert document == {"error": code, "message": "something happened"}
    assert body == canonical(document)


def test_error_body_truncates_an_overlong_message(contract):
    document = json.loads(routing.error_body("internal", "x" * 500))
    contract.validate_error(document)
    assert len(document["message"]) <= 200


def test_error_body_is_ascii_only(contract):
    body = routing.error_body("internal", "café")
    assert body.isascii()
    contract.validate_error(json.loads(body))
