from __future__ import annotations

import asyncio
import gzip
import json
import shutil
from contextlib import ExitStack
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest

from api_fake_binding import FakeR2Binding, RaisingBinding
from api_targets import EnvScope, UvicornThread, drain
from syncapi.app import BUCKET_BINDING, COMMIT_RE, create_app
from syncapi.plain import handle
from syncapi.storage import LocalPubStore

COMMIT = "0123456789abcdef0123456789abcdef01234567"
COMPARED = ("content-type", "cache-control", "x-api-commit", "x-content-type-options", "retry-after", "allow", "x-ul-to-revision", "etag")


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body)


def norm_headers(items) -> dict[str, str]:
    out = {k.lower(): v for k, v in items}
    if "content-type" in out:
        out["content-type"] = out["content-type"].split(";")[0].strip()
    return {k: out[k] for k in COMPARED if k in out}


class Rig:
    """one env, two implementations: the FastAPI app under uvicorn and plain.handle() called directly"""
    def __init__(self, env, stack: ExitStack) -> None:
        self.env = env
        self.stack = stack
        self._server = None

    def fastapi(self, path, *, method="GET", headers=None) -> Resp:
        if self._server is None:
            self._server = self.stack.enter_context(UvicornThread(EnvScope(create_app(), self.env)))
        return http_get(self._server.base_url, path, method, headers)

    def plain(self, path, *, method="GET", headers=None) -> Resp:
        async def go():
            r = await handle(method, f"http://127.0.0.1:1{path}", headers or {}, self.env)
            body = await drain(r.stream) if r.stream is not None else (r.body or b"")
            return Resp(r.status, norm_headers(r.headers), body)

        return asyncio.run(go())

    def get(self, impl, path, **kw) -> Resp:
        return getattr(self, impl)(path, **kw)


def http_get(base_url, path, method="GET", headers=None) -> Resp:
    with httpx.Client(base_url=base_url, timeout=10, follow_redirects=False, headers={"Accept-Encoding": "identity"}) as client:
        r = client.request(method, path, headers=headers or {})
    return Resp(r.status_code, norm_headers(r.headers.items()), r.content)


@pytest.fixture
def make_rig(request):
    stack = ExitStack()
    request.addfinalizer(stack.close)

    def make(env=None, *, root=None, commit="dev", binding=None):
        if env is None:
            env = SimpleNamespace(**{BUCKET_BINDING: binding or FakeR2Binding(root), "API_COMMIT": commit})
        return Rig(env, stack)

    return make


@pytest.fixture
def store_copy(store, tmp_path):
    root = tmp_path / "store"
    shutil.copytree(store.root, root)
    return root


IMPLS = ["fastapi", "plain"]


def assert_error(resp: Resp, status: int, code: str, commit="dev"):
    assert resp.status == status
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-api-commit"] == commit
    assert resp.headers["x-content-type-options"] == "nosniff"
    body = resp.json()
    assert body["error"] == code and isinstance(body["message"], str) and len(body["message"]) <= 200


# failure in store
@pytest.mark.parametrize("impl", IMPLS)
@pytest.mark.parametrize("path", ["/v1/version", "/v1/manifests/2", "/v1/updates/1", "/v1/snapshots/2", "/v1/lineage/2"])
def test_store_failure_is_500_internal(make_rig, contract, store, impl, path):
    resp = make_rig(binding=RaisingBinding()).get(impl, path)
    assert_error(resp, 500, "internal")
    contract.validate_error(resp.json())


def test_store_whose_get_raises_is_500_on_the_fastapi_app_with_an_explicit_store(contract):
    class Broken:
        async def get(self, key, *, if_none_match=None):
            raise RuntimeError("boom")

    with UvicornThread(create_app(Broken(), api_commit="dev")) as server:
        for path in ("/v1/version", "/v1/manifests/2", "/v1/updates/1"):
            resp = http_get(server.base_url, path)
            assert_error(resp, 500, "internal")
            contract.validate_error(resp.json())


def test_a_500_does_not_leak_the_exception_text():
    class Broken:
        async def get(self, key, *, if_none_match=None):
            raise RuntimeError("SECRET-INTERNAL-DETAIL")

    with UvicornThread(create_app(Broken(), api_commit="dev")) as server:
        assert b"SECRET-INTERNAL-DETAIL" not in http_get(server.base_url, "/v1/version").body


# no release
@pytest.mark.parametrize("impl", IMPLS)
@pytest.mark.parametrize("path", ["/v1/version", "/v1/updates/1", "/v1/updates/0"])
def test_missing_pointer_is_503_unavailable(make_rig, contract, store_copy, impl, path):
    (store_copy / "pub/current.json").unlink()
    resp = make_rig(root=store_copy).get(impl, path)
    assert_error(resp, 503, "unavailable")
    assert resp.headers["retry-after"] == "300"
    contract.validate_error(resp.json())


@pytest.mark.parametrize("impl", IMPLS)
def test_missing_pointer_still_serves_other_objects(make_rig, store_copy, impl):
    (store_copy / "pub/current.json").unlink()
    assert make_rig(root=store_copy).get(impl, "/v1/manifests/2").status == 200


@pytest.mark.parametrize("impl", IMPLS)
def test_missing_manifest_is_404_not_503(make_rig, store_copy, impl):
    resp = make_rig(root=store_copy).get(impl, "/v1/manifests/3")
    assert_error(resp, 404, "not_found")
    assert "retry-after" not in resp.headers


# corrupt pointer
CORRUPT = {
    "not-json": b"this is not json",
    "empty": b"",
    "array": b"[]",
    "string-revision": b'{"revision":"2","minimum_revision":1}',
    "bool-revision": b'{"revision":true,"minimum_revision":1}',
    "missing-minimum": b'{"revision":2}',
    "minimum-above-revision": b'{"revision":2,"minimum_revision":5}',
}


@pytest.mark.parametrize("impl", IMPLS)
@pytest.mark.parametrize("label", sorted(CORRUPT))
def test_corrupt_pointer_makes_updates_503_but_version_verbatim(make_rig, contract, store_copy, impl, label):
    (store_copy / "pub/current.json").write_bytes(CORRUPT[label])
    rig = make_rig(root=store_copy)
    updates = rig.get(impl, "/v1/updates/1")
    assert_error(updates, 503, "unavailable")
    assert updates.headers["retry-after"] == "300"
    contract.validate_error(updates.json())
    version = rig.get(impl, "/v1/version")
    assert version.status == 200 and version.body == CORRUPT[label]
    assert version.headers["cache-control"] == "public, max-age=300"


# package missing
@pytest.mark.parametrize("impl", IMPLS)
def test_deleted_package_gives_requires_snapshot_package_missing(make_rig, contract, store_copy, impl):
    (store_copy / "pub/updates/1-2.json.gz").unlink()
    resp = make_rig(root=store_copy).get(impl, "/v1/updates/1")
    assert resp.status == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers["cache-control"] == "public, max-age=300"
    assert resp.headers["x-ul-to-revision"] == "2"
    assert resp.headers["x-api-commit"] == "dev"
    document = resp.json()
    contract.validate("requires_snapshot", document)
    assert document == {"requires_snapshot": True, "snapshot_revision": 2, "from_revision": 1, "reason": "package_missing"}
    assert resp.headers["etag"].startswith('"') and resp.headers["etag"].endswith('"')


@pytest.mark.parametrize("impl", IMPLS)
def test_requires_snapshot_answers_304_on_a_matching_etag(make_rig, store_copy, impl):
    (store_copy / "pub/updates/1-2.json.gz").unlink()
    rig = make_rig(root=store_copy)
    first = rig.get(impl, "/v1/updates/1")
    again = rig.get(impl, "/v1/updates/1", headers={"If-None-Match": first.headers["etag"]})
    assert again.status == 304 and again.body == b""
    assert again.headers["etag"] == first.headers["etag"]
    assert again.headers["x-ul-to-revision"] == "2"
    other = rig.get(impl, "/v1/updates/1", headers={"If-None-Match": '"other"'})
    assert other.status == 200


@pytest.mark.parametrize("impl", IMPLS)
def test_below_minimum_is_decided_before_the_store_is_asked_for_a_package(make_rig, store_copy, impl):
    rig = make_rig(root=store_copy)
    resp = rig.get(impl, "/v1/updates/0")
    assert resp.json() == {"requires_snapshot": True, "snapshot_revision": 2, "from_revision": 0, "reason": "below_minimum"}
    requested = rig.env.BUCKET.requested
    assert requested and set(requested) == {"pub/current.json"}


@pytest.mark.parametrize("impl", IMPLS)
def test_updates_reads_at_most_two_objects(make_rig, store_copy, impl):
    rig = make_rig(root=store_copy)
    assert rig.get(impl, "/v1/updates/1").status == 200
    assert rig.env.BUCKET.requested == ["pub/current.json", "pub/updates/1-2.json.gz"]


@pytest.mark.parametrize("impl", IMPLS)
def test_a_release_with_a_higher_revision_moves_the_decision(make_rig, store_copy, impl):
    """the decision follows pub/current.json: bump it to revision 5 / minimum 3"""
    current = json.loads((store_copy / "pub/current.json").read_bytes())
    current.update(revision=5, minimum_revision=3)
    (store_copy / "pub/current.json").write_bytes(json.dumps(current).encode())
    (store_copy / "pub/updates/3-5.json.gz").write_bytes(gzip.compress(b"{}", mtime=0))
    rig = make_rig(root=store_copy)
    assert rig.get(impl, "/v1/updates/2").json()["reason"] == "below_minimum"
    assert rig.get(impl, "/v1/updates/2").json()["snapshot_revision"] == 5
    ok = rig.get(impl, "/v1/updates/3")
    assert ok.status == 200 and ok.headers["content-type"] == "application/gzip" and ok.headers["x-ul-to-revision"] == "5"
    assert rig.get(impl, "/v1/updates/4").json()["reason"] == "package_missing"
    assert_error(rig.get(impl, "/v1/updates/5"), 404, "no_update")
    assert_error(rig.get(impl, "/v1/updates/6"), 404, "no_update")


# no generated docs
@pytest.mark.parametrize("impl", IMPLS)
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_no_generated_docs(make_rig, store, impl, path):
    assert_error(make_rig(root=store.root).get(impl, path), 404, "not_found")


# route shape
def find_fastapi(app):
    seen = 0
    while app is not None and seen < 10:
        if hasattr(app, "routes") and hasattr(app, "openapi_url"):
            return app
        app = getattr(app, "fastapi", None) or getattr(app, "app", None)
        seen += 1
    raise AssertionError("could not find the FastAPI instance behind create_app()")


def test_route_shape(store):
    from fastapi.routing import APIRoute

    fastapi_app = find_fastapi(create_app(LocalPubStore(store.root), api_commit="dev"))
    routes = [r for r in fastapi_app.routes if isinstance(r, APIRoute)]
    assert [r.path for r in routes] == [
        "/v1/version", "/v1/manifests/{revision}", "/v1/updates/{from_revision}", "/v1/snapshots/{revision}",
        "/v1/icons/packs/{name}", "/v1/icons/{kind}/{sha256}", "/v1/lineage/{revision}",
    ]
    assert len(fastapi_app.routes) == len(routes), "no extra routes (docs, redoc, openapi)"
    for route in routes:
        assert route.methods == {"GET"}, route.path
        assert route.response_model is None, route.path
        dependant = route.dependant
        for kind in ("path_params", "query_params", "header_params", "cookie_params", "body_params", "dependencies"):
            assert getattr(dependant, kind) == [], f"{route.path}: {kind}"


def test_fastapi_redirect_slashes_and_docs_are_off(store):
    fastapi_app = find_fastapi(create_app(LocalPubStore(store.root), api_commit="dev"))
    assert fastapi_app.router.redirect_slashes is False
    assert fastapi_app.docs_url is None and fastapi_app.redoc_url is None and fastapi_app.openapi_url is None


# commit validation
@pytest.mark.parametrize("bad", ["xyz", "DEV", "", "abc", "A" * 40, "g" * 40, "a" * 39, "a" * 41, "dev ", "dev\n"])
def test_explicit_invalid_commit_raises(bad, store):
    with pytest.raises(ValueError):
        create_app(LocalPubStore(store.root), api_commit=bad)


@pytest.mark.parametrize("good", ["dev", COMMIT, "f" * 40, "0" * 40])
def test_explicit_valid_commit_is_accepted_and_served(store, good):
    with UvicornThread(create_app(LocalPubStore(store.root), api_commit=good)) as server:
        assert http_get(server.base_url, "/v1/version").headers["x-api-commit"] == good
        assert http_get(server.base_url, "/nope").headers["x-api-commit"] == good


@pytest.mark.parametrize("impl", IMPLS)
@pytest.mark.parametrize("value", ["bad", "A" * 40, "", "x" * 41, 7, None])
def test_invalid_env_commit_is_replaced_by_dev(make_rig, store, impl, value):
    env = SimpleNamespace(**{BUCKET_BINDING: FakeR2Binding(store.root), "API_COMMIT": value})
    resp = make_rig(env).get(impl, "/v1/version")
    assert resp.status == 200 and resp.headers["x-api-commit"] == "dev"


@pytest.mark.parametrize("impl", IMPLS)
def test_missing_env_commit_is_dev(make_rig, store, impl):
    env = SimpleNamespace(**{BUCKET_BINDING: FakeR2Binding(store.root)})
    assert make_rig(env).get(impl, "/v1/version").headers["x-api-commit"] == "dev"


@pytest.mark.parametrize("impl", IMPLS)
def test_env_commit_40_hex_is_the_header_on_every_response(make_rig, store, impl):
    rig = make_rig(root=store.root, commit=COMMIT)
    for path, method in [("/v1/version", "GET"), ("/v1/manifests/99", "GET"), ("/nothing", "GET"), ("/v1/version", "POST"), ("/v1/updates/2", "GET")]:
        assert rig.get(impl, path, method=method).headers["x-api-commit"] == COMMIT, (method, path)


def test_explicit_commit_wins_over_env(store):
    env = SimpleNamespace(**{BUCKET_BINDING: FakeR2Binding(store.root), "API_COMMIT": COMMIT})
    with UvicornThread(EnvScope(create_app(api_commit="dev"), env)) as server:
        assert http_get(server.base_url, "/v1/version").headers["x-api-commit"] == "dev"


def test_commit_pattern_is_forty_lowercase_hex_or_dev():
    assert COMMIT_RE.fullmatch("dev") and COMMIT_RE.fullmatch(COMMIT)
    assert not COMMIT_RE.fullmatch("DEV") and not COMMIT_RE.fullmatch(COMMIT.upper())


def test_binding_name_is_BUCKET():
    assert BUCKET_BINDING == "BUCKET"


# local
def test_local_factory_without_store_root_exits(monkeypatch):
    from syncapi.local import create_local_app

    monkeypatch.delenv("UL_HOUSE_STORE_ROOT", raising=False)
    with pytest.raises(SystemExit):
        create_local_app()


def test_local_factory_root_without_pub_exits(monkeypatch, tmp_path):
    from syncapi.local import create_local_app

    monkeypatch.setenv("UL_HOUSE_STORE_ROOT", str(tmp_path))
    with pytest.raises(SystemExit):
        create_local_app()


def test_local_factory_missing_directory_exits(monkeypatch, tmp_path):
    from syncapi.local import create_local_app

    monkeypatch.setenv("UL_HOUSE_STORE_ROOT", str(tmp_path / "nowhere"))
    with pytest.raises(SystemExit):
        create_local_app()


def test_local_factory_serves_the_store(monkeypatch, store):
    from syncapi.local import create_local_app

    monkeypatch.setenv("UL_HOUSE_STORE_ROOT", str(store.root))
    monkeypatch.delenv("UL_HOUSE_API_COMMIT", raising=False)
    with UvicornThread(create_local_app()) as server:
        resp = http_get(server.base_url, "/v1/version")
    assert resp.status == 200 and resp.body == (store.root / "pub/current.json").read_bytes()
    assert resp.headers["x-api-commit"] == "dev"


def test_local_factory_commit_comes_from_the_environment(monkeypatch, store):
    from syncapi.local import create_local_app

    monkeypatch.setenv("UL_HOUSE_STORE_ROOT", str(store.root))
    monkeypatch.setenv("UL_HOUSE_API_COMMIT", COMMIT)
    with UvicornThread(create_local_app()) as server:
        assert http_get(server.base_url, "/v1/version").headers["x-api-commit"] == COMMIT


# plain handler fastapi app
SCENARIOS = {
    "healthy": lambda root: None,
    "no-pointer": lambda root: (root / "pub/current.json").unlink(),
    "corrupt-pointer": lambda root: (root / "pub/current.json").write_bytes(b"{nope"),
    "no-package": lambda root: (root / "pub/updates/1-2.json.gz").unlink(),
}
PATHS = [
    "/v1/version", "/v1/manifests/2", "/v1/manifests/9", "/v1/updates/0", "/v1/updates/1", "/v1/updates/2", "/v1/updates/3",
    "/v1/snapshots/2", "/v1/lineage/2", "/v1/icons/packs/base-2.zip", "/v1/icons/packs/nope.zip", "/nothing", "/v1/updates/x",
]


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_plain_equals_fastapi_for_every_route(make_rig, store, tmp_path, scenario):
    root = tmp_path / "store"
    shutil.copytree(store.root, root)
    SCENARIOS[scenario](root)
    rig = make_rig(root=root, commit=COMMIT)
    for path in PATHS:
        a, b = rig.fastapi(path), rig.plain(path)
        assert (a.status, a.headers, a.body) == (b.status, b.headers, b.body), path


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_plain_equals_fastapi_for_other_methods(make_rig, store, method):
    rig = make_rig(root=store.root)
    for path in ("/v1/version", "/v1/manifests/2", "/nothing"):
        a, b = rig.fastapi(path, method=method), rig.plain(path, method=method)
        assert (a.status, a.headers, a.body) == (b.status, b.headers, b.body)
        assert a.status == 405 and a.headers["allow"] == "GET"


def test_plain_equals_fastapi_for_conditional_requests(make_rig, store):
    rig = make_rig(root=store.root)
    for path in ("/v1/version", "/v1/manifests/2", "/v1/updates/1", "/v1/updates/0"):
        etag = rig.fastapi(path).headers["etag"]
        a = rig.fastapi(path, headers={"If-None-Match": etag})
        b = rig.plain(path, headers={"If-None-Match": etag})
        c = rig.plain(path, headers={"if-none-match": etag})
        assert a.status == b.status == c.status == 304
        assert (a.headers, a.body) == (b.headers, b.body) == (c.headers, c.body)


def test_plain_never_reads_outside_the_allow_list(make_rig, store):
    rig = make_rig(root=store.root)
    for path in PATHS + ["/v1/manifests/..%2F..%2Fbuild%2Fseed%2Fcurrent", "/v1/icons/packs/..%2Fsecret", "/v1/lineage/%2e%2e", "/v1/snapshots/2%00"]:
        rig.plain(path)
    assert all(key.startswith("pub/") for key in rig.env.BUCKET.requested)
