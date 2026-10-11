"""only pub/ is readable"""
from __future__ import annotations

import http.client
import shutil
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from api_fake_binding import FakeR2Binding
from api_invariants import api_path
from api_targets import EnvScope, PlainASGI, UvicornThread
from syncapi.app import create_app
from syncapi.routing import is_allowed_key
from syncapi.storage import LocalPubStore

MARKERS = (b"DECOY-BUILD-SEED", b"DECOY-BUILD-HISTORY", b"DECOY-CANDIDATE", b"DECOY-ROOT")

ENCODED_TRAVERSAL = [
    "/v1/manifests/..%2F..%2Fbuild%2Fseed%2Fcurrent",
    "/v1/icons/packs/..%2F..%2F..%2Fbuild%2Fseed%2Fcurrent.sqlite",
    "/v1/icons/..%2F..%2Fcandidates/3",
    "/v1/lineage/%2e%2e",
    "/v1/snapshots/2%00",
    "/v1/manifests/%2e%2e%2f%2e%2e%2fsecret.txt",
    "/v1/manifests/3%2F..%2F..%2Fcandidates%2F3%2Fmanifests%2F3",
    "/v1/icons/equipment/..%2F..%2F..%2Fbuild%2Fhistory%2Fcheckpoint.json",
    "/v1/updates/..%2Fsecret.txt",
    "/v1/updates/%2e%2e",
    "/v1/icons/packs/%2e%2e%2f%2e%2e%2fsecret.txt",
    "/v1/manifests/..%5Cbuild",
    "/v1/manifests/2%0a",
    "/v1/manifests/%00",
    "/v1/icons/packs/base-2.zip%00.png",
    "/v1/manifests/%252e%252e%252fsecret.txt",
    "/v1/version%2F..%2F..%2Fsecret.txt",
    "/v1%2Fmanifests%2F2",
]

LITERAL_TRAVERSAL = [
    "/v1/manifests/../../build/seed/current.sqlite",
    "/v1/../build/seed/current.sqlite",
    "/v1/icons/packs/../../../secret.txt",
    "/../secret.txt",
    "/v1/manifests/../current.json",
    "/v1/lineage/../../candidates/3/manifests/3.json",
    "/v1/snapshots/..",
    "/v1/icons/equipment/../../../build/history/checkpoint.json",
    "/../../../../../../etc/passwd",
]


def assert_no_marker(body: bytes, where: str):
    for marker in MARKERS:
        assert marker not in body, f"{where} leaked {marker!r}"


def raw_get(base_url: str, path: str) -> tuple[int, dict[str, str], bytes]:
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        conn.request("GET", path, headers={"Accept-Encoding": "identity"})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


# against every target
@pytest.mark.parametrize("path", ENCODED_TRAVERSAL)
def test_encoded_traversal_is_404_and_leaks_nothing(target, path):
    status, headers, body = raw_get(target.base_url, path)
    assert status == 404, f"{path}: {status}"
    assert headers["content-type"].split(";")[0] == "application/json"
    assert_no_marker(body, path)


@pytest.mark.parametrize("path", LITERAL_TRAVERSAL)
def test_literal_dot_dot_paths_are_404_and_leak_nothing(target, path):
    status, _, body = raw_get(target.base_url, path)
    assert status == 404, f"{path}: {status}"
    assert_no_marker(body, path)
    assert b"SQLite" not in body and b'"revision"' not in body


def test_a_decoy_named_like_a_contract_object_is_not_served(target):
    """candidates/3/manifests/3.json exists next to pub/ -- /v1/manifests/3 must still be unknown"""
    status, _, body = raw_get(target.base_url, "/v1/manifests/3")
    assert status == 404
    assert_no_marker(body, "/v1/manifests/3")


# decoy-derived param per route
DECOY_PARAMS = [
    "build", "seed", "current.sqlite", "secret.txt", "candidates", "3", "3.json", "current", "checkpoint.json", "history",
    "build/seed/current.sqlite", "build%2Fseed%2Fcurrent.sqlite", "..%2Fbuild%2Fseed%2Fcurrent.sqlite", "..", "../secret.txt",
    "candidates/3/manifests/3.json", "pub", "pub/current.json", "current.json", "DECOY-ROOT",
]

ROUTES = [
    "/v1/manifests/{a}", "/v1/updates/{a}", "/v1/snapshots/{a}", "/v1/icons/packs/{a}", "/v1/lineage/{a}",
]


def test_no_route_serves_decoy_bytes_for_any_decoy_derived_parameter(target, http):
    checked = 0
    for template in ROUTES:
        for value in DECOY_PARAMS:
            r = http.get(template.format(a=value))
            assert_no_marker(r.content, template.format(a=value))
            assert r.status_code == 404 or (target.name == "live" and value == "3"), (template, value, r.status_code)
            checked += 1
    for kind in DECOY_PARAMS:
        for sha in DECOY_PARAMS:
            r = http.get(f"/v1/icons/{kind}/{sha}")
            assert_no_marker(r.content, f"icons/{kind}/{sha}")
            assert r.status_code == 404
            checked += 1
    assert checked > 400


# store without pointer
@contextmanager
def three_apps(root):
    bindings = {"fastapi_r2": FakeR2Binding(root), "plain": FakeR2Binding(root)}
    env = lambda name: SimpleNamespace(BUCKET=bindings[name], API_COMMIT="dev")
    with (
        UvicornThread(create_app(LocalPubStore(root), api_commit="dev")) as a,
        UvicornThread(EnvScope(create_app(), env("fastapi_r2"))) as b,
        UvicornThread(PlainASGI(env("plain"))) as c,
    ):
        yield {"fastapi": a.base_url, "fastapi_r2": b.base_url, "plain": c.base_url}, bindings


def happy_path_walk(store, base_url):
    with httpx.Client(base_url=base_url, timeout=10) as client:
        for relative in store.objects:
            client.get(api_path(relative))
        for from_revision in range(0, store.revision + 2):
            client.get(f"/v1/updates/{from_revision}")
        client.get("/v1/version", headers={"If-None-Match": "*"})
        client.get(f"/v1/manifests/{store.revision}", headers={"If-None-Match": '"x"'})


def test_bucket_keys_requested_by_the_worker_implementations_are_all_allow_listed(store):
    with three_apps(store.root) as (urls, bindings):
        for name in ("fastapi_r2", "plain"):
            happy_path_walk(store, urls[name])
            for path in ENCODED_TRAVERSAL:
                raw_get(urls[name], path)
            for path in LITERAL_TRAVERSAL:
                raw_get(urls[name], path)
            requested = bindings[name].requested
            assert requested, f"{name} never asked the bucket for anything"
            for key in requested:
                assert key.startswith("pub/"), f"{name} asked for {key!r}"
                assert is_allowed_key(key), f"{name} asked for {key!r}"


def test_the_two_implementations_ask_for_the_same_keys(store):
    with three_apps(store.root) as (urls, bindings):
        for name in ("fastapi_r2", "plain"):
            happy_path_walk(store, urls[name])
            for path in ENCODED_TRAVERSAL + LITERAL_TRAVERSAL:
                raw_get(urls[name], path)
        assert sorted(bindings["fastapi_r2"].requested) == sorted(bindings["plain"].requested)


def test_without_pub_objects_nothing_falls_back_to_build_or_candidates(store, tmp_path):
    root = tmp_path / "store"
    shutil.copytree(store.root, root)
    (root / "pub/current.json").unlink()
    (root / "pub/manifests/2.json").unlink()
    for relative, data in {
        "current.json": b"DECOY-ROOT", "manifests/2.json": b"DECOY-ROOT", "build/current.json": b"DECOY-BUILD-SEED",
        "build/manifests/2.json": b"DECOY-BUILD-SEED", "candidates/2/current.json": b"DECOY-CANDIDATE",
        "candidates/2/manifests/2.json": b"DECOY-CANDIDATE", "build/pub/current.json": b"DECOY-BUILD-HISTORY",
    }.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)
    with three_apps(root) as (urls, bindings):
        for name, base_url in urls.items():
            with httpx.Client(base_url=base_url, timeout=10) as client:
                version = client.get("/v1/version")
                manifest = client.get("/v1/manifests/2")
                updates = client.get("/v1/updates/1")
            assert version.status_code == 503 and updates.status_code == 503, name
            assert manifest.status_code == 404, name
            for r in (version, manifest, updates):
                assert_no_marker(r.content, name)
        for name in ("fastapi_r2", "plain"):
            assert all(key.startswith("pub/") for key in bindings[name].requested)


def test_symlink_in_pub_to_a_decoy_is_not_served_by_the_local_store(store, tmp_path):
    root = tmp_path / "store"
    shutil.copytree(store.root, root)
    (root / "pub/manifests/9.json").symlink_to(root / "build/seed/current.sqlite")
    (root / "pub/lineage/9.json.gz").symlink_to(root / "secret.txt")
    with UvicornThread(create_app(LocalPubStore(root), api_commit="dev")) as server:
        for path in ("/v1/manifests/9", "/v1/lineage/9"):
            r = httpx.get(server.base_url + path, timeout=10)
            assert r.status_code == 404
            assert_no_marker(r.content, path)
