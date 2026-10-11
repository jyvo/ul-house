"""one set of assertions, run against every implementation of the v1 contract"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from api_invariants import api_path, check_current, check_manifest, check_pack, check_package

IMMUTABLE = "public, max-age=31536000, immutable"
POINTER = "public, max-age=300"
COMMIT_PATTERN = re.compile(r"^([0-9a-f]{40}|dev)$")
ETAG_PATTERN = re.compile(r'^"[!#-~]{1,128}"$')
REV_PATTERN = re.compile(r"^[1-9][0-9]{0,14}$")


# helpers
def assert_api_headers(r, target, *, error=False):
    commit = r.headers.get("x-api-commit")
    assert commit is not None and COMMIT_PATTERN.fullmatch(commit), f"X-API-Commit: {commit!r}"
    if target.expected_commit:
        assert commit == target.expected_commit
    assert r.headers.get("x-content-type-options") == "nosniff", f"{r.status_code}: nosniff"
    assert "set-cookie" not in r.headers
    assert "access-control-allow-origin" not in r.headers
    if error:
        assert r.headers["content-type"].split(";")[0] == "application/json"
        assert r.headers["cache-control"] == "no-store"


def assert_error(r, target, status, code, contract):
    assert r.status_code == status, f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:120]}"
    assert_api_headers(r, target, error=True)
    body = r.json()
    contract.validate_error(body)
    assert body["error"] == code


def assert_ok(r, target, content_type, cache_control):
    assert r.status_code == 200, f"{r.request.url.path}: {r.status_code} {r.text[:120]}"
    assert_api_headers(r, target)
    assert r.headers["content-type"].split(";")[0] == content_type
    assert r.headers["cache-control"] == cache_control
    assert ETAG_PATTERN.fullmatch(r.headers["etag"]), r.headers["etag"]
    assert "content-encoding" not in r.headers
    if "content-length" in r.headers:
        assert int(r.headers["content-length"]) == len(r.content)
    elif target.name != "live":
        raise AssertionError("every 200 carries Content-Length")


def assert_not_modified(http, target, path, first, cache_control):
    r = http.get(path, headers={"If-None-Match": first.headers["etag"]})
    assert r.status_code == 304, f"{path}: {r.status_code}"
    assert r.content == b""
    assert_api_headers(r, target)
    assert r.headers["etag"] == first.headers["etag"]
    assert r.headers["cache-control"] == cache_control
    other = http.get(path, headers={"If-None-Match": '"not-the-etag"'})
    assert other.status_code == 200


def open_zip(data: bytes):
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = archive.namelist()
    return json.loads(archive.read("index.json")), {n: archive.read(n) for n in names}, names


@pytest.fixture(scope="module")
def served(target):
    with httpx.Client(base_url=target.base_url, timeout=10, headers={"Accept-Encoding": "identity", "User-Agent": "ul-house-api-conformance"}) as client:
        raw = client.get("/v1/version")
        assert raw.status_code == 200, "the target serves no release"
        current = raw.json()
        manifest_raw = client.get(f"/v1/manifests/{current['revision']}")
        manifest = manifest_raw.json()
        packs = {}
        for ref in [manifest["artifacts"]["icon_packs"]["base"], *manifest["artifacts"]["icon_packs"]["deltas"]]:
            packs[ref["path"]] = client.get("/v1/" + ref["path"]).content
    return SimpleNamespace(current=current, current_bytes=raw.content, manifest=manifest, manifest_bytes=manifest_raw.content, packs=packs)


def pick(items, target, live_count):
    return items[:live_count] if target.name == "live" and len(items) > live_count else items


# version
def test_version_headers(http, target, contract):
    r = http.get("/v1/version")
    assert_ok(r, target, "application/json", POINTER)


def test_version_body_is_valid_and_consistent(http, contract):
    current = http.get("/v1/version").json()
    contract.validate("current", current)
    check_current(current)


def test_version_conditional_get(http, target):
    path = "/v1/version"
    first = http.get(path)
    assert_not_modified(http, target, path, first, POINTER)


def test_version_etag_is_stable(http):
    assert http.get("/v1/version").headers["etag"] == http.get("/v1/version").headers["etag"]


def test_version_signature_verifies(target, served):
    if not target.public_keys:
        pytest.skip("no public keys configured for this target")
    doc = served.current
    body = {k: v for k, v in doc.items() if k != "sig"}
    message = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    signature = base64.b64decode(doc["sig"], validate=True)
    assert len(signature) == 64
    for key in target.public_keys:
        try:
            Ed25519PublicKey.from_public_bytes(key).verify(signature, message)
            return
        except InvalidSignature:
            continue
    raise AssertionError("no configured public key verifies the signature")


def test_version_is_the_stored_bytes(target, served):
    if target.store_root is None:
        pytest.skip("no store to compare with")
    assert served.current_bytes == (target.store_root / "pub/current.json").read_bytes()


def test_every_stored_object_is_served_byte_for_byte(target, http):
    if target.store_root is None:
        pytest.skip("no store to compare with")
    objects = sorted(p.relative_to(target.store_root / "pub").as_posix() for p in (target.store_root / "pub").rglob("*") if p.is_file())
    assert len(objects) >= 8
    for relative in objects:
        r = http.get(api_path(relative))
        data = (target.store_root / "pub" / relative).read_bytes()
        if relative.startswith("updates/") and not relative.endswith(f"-{http_revision(http)}.json.gz"):
            continue
        assert r.status_code == 200, relative
        assert r.content == data, relative
        assert "content-encoding" not in r.headers, relative


def http_revision(http) -> int:
    return http.get("/v1/version").json()["revision"]


# manifests
def test_current_manifest(http, target, contract, served):
    n = served.current["revision"]
    r = http.get(f"/v1/manifests/{n}")
    assert_ok(r, target, "application/json", IMMUTABLE)
    assert hashlib.sha256(r.content).hexdigest() == served.current["manifest_sha256"]
    manifest = r.json()
    contract.validate("manifest", manifest)
    check_manifest(manifest, served.current)


def test_manifest_conditional_get(http, target, served):
    path = f"/v1/manifests/{served.current['revision']}"
    assert_not_modified(http, target, path, http.get(path), IMMUTABLE)


def test_manifest_is_byte_identical_on_refetch(http, served):
    path = f"/v1/manifests/{served.current['revision']}"
    a, b = http.get(path), http.get(path)
    assert a.content == b.content and a.headers["etag"] == b.headers["etag"]


def test_manifest_unknown_revision_is_404(http, target, contract, served):
    assert_error(http.get(f"/v1/manifests/{served.current['revision'] + 1000}"), target, 404, "not_found", contract)


@pytest.mark.parametrize("value", ["0", "01", "-1", "1.0", "abc", "%31", "2%20", "1e3", "2.json", "0x2", "+2", "1000000000000000"])
def test_manifest_malformed_revision_is_404(http, target, contract, value):
    assert_error(http.get(f"/v1/manifests/{value}"), target, 404, "not_found", contract)


# updates
def test_updates_from_zero_requires_snapshot(http, target, contract, served):
    n = served.current["revision"]
    r = http.get("/v1/updates/0")
    assert_ok(r, target, "application/json", POINTER)
    body = r.json()
    contract.validate("requires_snapshot", body)
    assert body["reason"] == "below_minimum" and body["snapshot_revision"] == n and body["from_revision"] == 0
    assert r.headers["x-ul-to-revision"] == str(n)
    assert REV_PATTERN.fullmatch(r.headers["x-ul-to-revision"])


def test_updates_packages(http, target, contract, served):
    n = served.current["revision"]
    updates = pick(served.manifest["artifacts"]["updates"], target, 1)
    if target.name == "live" and len(served.manifest["artifacts"]["updates"]) > 1:
        updates = [served.manifest["artifacts"]["updates"][0], served.manifest["artifacts"]["updates"][-1]]
    if not updates:
        pytest.skip("snapshot-only release: no update packages")
    for ref in updates:
        r = http.get(f"/v1/updates/{ref['from_revision']}")
        assert_ok(r, target, "application/gzip", POINTER)
        assert r.headers["x-ul-to-revision"] == str(n)
        assert hashlib.sha256(r.content).hexdigest() == ref["sha256"]
        assert len(r.content) == ref["bytes"]
        package = json.loads(gzip.decompress(r.content))
        contract.validate("update_package", package)
        delta = next((d for d in served.manifest["artifacts"]["icon_packs"]["deltas"] if d["from_revision"] == ref["from_revision"]), None)
        index = open_zip(served.packs[delta["path"]])[0] if delta else None
        check_package(package, served.manifest, index)


def test_updates_conditional_get_on_a_package(http, target, served):
    updates = served.manifest["artifacts"]["updates"]
    if not updates:
        pytest.skip("snapshot-only release")
    path = f"/v1/updates/{updates[0]['from_revision']}"
    assert_not_modified(http, target, path, http.get(path), POINTER)


def test_updates_conditional_get_on_requires_snapshot(http, target):
    assert_not_modified(http, target, "/v1/updates/0", http.get("/v1/updates/0"), POINTER)


def test_updates_below_minimum(http, target, contract, served):
    m, n = served.current["minimum_revision"], served.current["revision"]
    if m <= 1:
        pytest.skip("minimum_revision is 1: nothing between 1 and the minimum")
    for from_revision in pick(list(range(1, m)), target, 3):
        r = http.get(f"/v1/updates/{from_revision}")
        assert_ok(r, target, "application/json", POINTER)
        body = r.json()
        contract.validate("requires_snapshot", body)
        assert (body["reason"], body["snapshot_revision"], body["from_revision"]) == ("below_minimum", n, from_revision)


@pytest.mark.parametrize("offset", [0, 1])
def test_updates_at_or_beyond_current_is_no_update(http, target, contract, served, offset):
    r = http.get(f"/v1/updates/{served.current['revision'] + offset}")
    assert_error(r, target, 404, "no_update", contract)


@pytest.mark.parametrize("value", ["01", "-1", "1.5", "abc", "%31", "1%20", "1e2", "", "1/2", "1000000000000000"])
def test_updates_malformed_is_404_not_found(http, target, contract, value):
    assert_error(http.get(f"/v1/updates/{value}"), target, 404, "not_found", contract)


def test_snapshot_only_release_answers_requires_snapshot_for_every_older_revision(http, target, contract, served):
    if not served.manifest["snapshot_only"]:
        pytest.skip("not a snapshot-only release")
    n = served.current["revision"]
    for from_revision in range(0, n):
        r = http.get(f"/v1/updates/{from_revision}")
        assert r.headers["content-type"].split(";")[0] == "application/json"
        contract.validate("requires_snapshot", r.json())


# snapshots
def test_current_snapshot(http, target, served):
    ref = served.manifest["artifacts"]["snapshot"]
    r = http.get(f"/v1/snapshots/{served.current['revision']}")
    assert_ok(r, target, "application/gzip", IMMUTABLE)
    assert hashlib.sha256(r.content).hexdigest() == ref["sha256"] and len(r.content) == ref["bytes"]
    assert r.content[:2] == b"\x1f\x8b", "served gzip bytes unchanged"


def test_snapshot_conditional_get(http, target, served):
    path = f"/v1/snapshots/{served.current['revision']}"
    assert_not_modified(http, target, path, http.get(path), IMMUTABLE)


def test_snapshot_unknown_is_404(http, target, contract, served):
    assert_error(http.get(f"/v1/snapshots/{served.current['revision'] + 1000}"), target, 404, "not_found", contract)


@pytest.mark.parametrize("value", ["0", "01", "-1", "abc", "%32", "2.sqlite.gz", "unison-2.sqlite.gz"])
def test_snapshot_malformed_is_404(http, target, contract, value):
    assert_error(http.get(f"/v1/snapshots/{value}"), target, 404, "not_found", contract)


# icons
def base_icons(served):
    return open_zip(served.packs[served.manifest["artifacts"]["icon_packs"]["base"]["path"]])[0]["icons"]


def test_icons_by_content_address(http, target, served):
    icons = pick(base_icons(served), target, 3)
    if not icons:
        pytest.skip("no icons in the release")
    for icon in icons:
        r = http.get(f"/v1/icons/{icon['kind']}/{icon['sha256']}")
        assert_ok(r, target, "image/png", IMMUTABLE)
        assert hashlib.sha256(r.content).hexdigest() == icon["sha256"]
        assert len(r.content) == icon["bytes"]


def test_icon_conditional_get(http, target, served):
    icons = base_icons(served)
    if not icons:
        pytest.skip("no icons in the release")
    path = f"/v1/icons/{icons[0]['kind']}/{icons[0]['sha256']}"
    assert_not_modified(http, target, path, http.get(path), IMMUTABLE)


def test_icon_wrong_kind_for_a_real_sha_is_404(http, target, contract, served):
    icons = base_icons(served)
    if not icons:
        pytest.skip("no icons in the release")
    sha = icons[0]["sha256"]
    for kind in ("item", "ability"):
        if kind != icons[0]["kind"]:
            assert_error(http.get(f"/v1/icons/{kind}/{sha}"), target, 404, "not_found", contract)


@pytest.mark.parametrize(
    "path",
    [
        "/v1/icons/equipment/" + "0" * 64, "/v1/icons/equipment/" + "A" * 64, "/v1/icons/equipment/" + "a" * 63, "/v1/icons/equipment/" + "a" * 64 + ".png",
        "/v1/icons/packs/" + "a" * 64, "/v1/icons/Equip/" + "a" * 64, "/v1/icons/skill/" + "a" * 64, "/v1/icons/equipment", "/v1/icons/equipment/",
        "/v1/icons", "/v1/icons/", "/v1/icons/weapon/" + "a" * 64,
    ],
)
def test_icon_bad_paths_are_404(http, target, contract, path):
    assert_error(http.get(path), target, 404, "not_found", contract)


# icon packs
def test_base_pack(http, target, contract, served):
    ref = served.manifest["artifacts"]["icon_packs"]["base"]
    r = http.get("/v1/" + ref["path"])
    assert_ok(r, target, "application/zip", IMMUTABLE)
    assert hashlib.sha256(r.content).hexdigest() == ref["sha256"] and len(r.content) == ref["bytes"]
    index, members, names = open_zip(r.content)
    contract.validate("icon_pack", index)
    assert index["pack_type"] == "base" and index["from_revision"] is None
    check_pack(index, members, names, served.manifest, ref["path"])


def test_delta_packs(http, target, contract, served):
    deltas = served.manifest["artifacts"]["icon_packs"]["deltas"]
    if not deltas:
        pytest.skip("no delta packs")
    for ref in deltas:
        r = http.get("/v1/" + ref["path"])
        assert_ok(r, target, "application/zip", IMMUTABLE)
        assert hashlib.sha256(r.content).hexdigest() == ref["sha256"] and len(r.content) == ref["bytes"]
        index, members, names = open_zip(r.content)
        contract.validate("icon_pack", index)
        assert index["pack_type"] == "delta" and index["from_revision"] == ref["from_revision"]
        check_pack(index, members, names, served.manifest, ref["path"])


def test_pack_conditional_get(http, target, served):
    path = "/v1/" + served.manifest["artifacts"]["icon_packs"]["base"]["path"]
    assert_not_modified(http, target, path, http.get(path), IMMUTABLE)


@pytest.mark.parametrize("name", ["base-2", "delta-1.zip", "base-0.zip", "base-02.zip", "delta-1-2", "unknown.zip", "base-999999.zip", "delta-998-999.zip", "", "index.json", "Base-2.zip", "base-2.zip.gz"])
def test_pack_bad_or_unknown_names_are_404(http, target, contract, name):
    assert_error(http.get(f"/v1/icons/packs/{name}"), target, 404, "not_found", contract)


# lineage
def test_current_lineage(http, target, served):
    ref = served.manifest["artifacts"]["lineage"]
    r = http.get(f"/v1/lineage/{served.current['revision']}")
    assert_ok(r, target, "application/gzip", IMMUTABLE)
    assert hashlib.sha256(r.content).hexdigest() == ref["sha256"] and len(r.content) == ref["bytes"]
    json.loads(gzip.decompress(r.content))


def test_lineage_conditional_get(http, target, served):
    path = f"/v1/lineage/{served.current['revision']}"
    assert_not_modified(http, target, path, http.get(path), IMMUTABLE)


def test_lineage_unknown_is_404(http, target, contract, served):
    assert_error(http.get(f"/v1/lineage/{served.current['revision'] + 1000}"), target, 404, "not_found", contract)


# global behaviour
GLOBAL_404 = [
    "/", "/v1", "/v1/", "/v2/version", "/v1/version/", "/v1/VERSION", "/v1/manifests", "/v1/manifests/", "/docs", "/redoc",
    "/openapi.json", "/robots.txt", "/favicon.ico", "/current.json", "/pub/current.json", "/v1/versions", "/v1/updates/", "/v1/snapshots",
    "/v1/lineage", "/v1//version", "/v1/version.json",
]


@pytest.mark.parametrize("path", GLOBAL_404)
def test_unknown_paths_are_404_not_found(http, target, contract, path):
    assert_error(http.get(path), target, 404, "not_found", contract)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
@pytest.mark.parametrize("path", ["/v1/version", "/v1/manifests/2", "/v1/updates/1", "/nothing"])
def test_other_methods_are_405(http, target, contract, method, path):
    r = http.request(method, path)
    assert_error(r, target, 405, "method_not_allowed", contract)
    assert r.headers["allow"] == "GET"


def test_error_bodies_are_small_json(http):
    r = http.get("/v1/manifests/0")
    assert len(r.content) < 400 and r.content.isascii()


def test_every_response_names_the_commit(http, target, served):
    paths = ["/v1/version", f"/v1/manifests/{served.current['revision']}", "/v1/updates/0", "/nope", "/v1/manifests/0"]
    for path in paths:
        assert_api_headers(http.get(path), target)


@pytest.mark.live
def test_live_commit_matches_the_expected_one(target):
    if target.name != "live" or not target.expected_commit:
        pytest.skip("not the live target, or UL_HOUSE_API_EXPECTED_COMMIT is unset")
    r = httpx.get(target.base_url + "/v1/version", timeout=10)
    assert r.headers["x-api-commit"] == target.expected_commit
