from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
API = HERE.parent
WORKER_SRC = API / "worker" / "src"

for entry in (WORKER_SRC, HERE / "fixtures", HERE):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from api_common import Contract

if any(importlib.util.find_spec(name) is None for name in ("fastapi", "uvicorn", "httpx", "jsonschema", "yaml")):
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture(scope="session")
def contract() -> Contract:
    return Contract()


@pytest.fixture(scope="session")
def store(tmp_path_factory):
    import make_pub

    return make_pub.build_pub(tmp_path_factory.mktemp("store"))


def _target(name, base_url, store, *, with_root=True, keys=None, commit="dev"):
    from api_targets import Target

    return Target(name, base_url, store.root if with_root else None, keys if keys is not None else (store.public_key,), commit)


@pytest.fixture(scope="session")
def fastapi_target(store):
    from api_targets import UvicornThread
    from syncapi.app import create_app
    from syncapi.storage import LocalPubStore

    with UvicornThread(create_app(LocalPubStore(store.root), api_commit="dev")) as server:
        yield _target("fastapi", server.base_url, store)


@pytest.fixture(scope="session")
def fastapi_r2_target(store):
    from api_fake_binding import FakeR2Binding
    from api_targets import EnvScope, UvicornThread
    from syncapi.app import create_app

    binding = FakeR2Binding(store.root)
    env = SimpleNamespace(BUCKET=binding, API_COMMIT="dev")
    with UvicornThread(EnvScope(create_app(), env)) as server:
        yield _target("fastapi_r2", server.base_url, store)


@pytest.fixture(scope="session")
def plain_target(store):
    from api_fake_binding import FakeR2Binding
    from api_targets import PlainASGI, UvicornThread

    env = SimpleNamespace(BUCKET=FakeR2Binding(store.root), API_COMMIT="dev")
    with UvicornThread(PlainASGI(env)) as server:
        yield _target("plain", server.base_url, store)


@pytest.fixture(scope="session")
def static_target(store, tmp_path_factory):
    import api_static_mock

    out = tmp_path_factory.mktemp("static")
    api_static_mock.render(store.root, out)
    with api_static_mock.StaticServer(out) as server:
        yield _target("static", server.base_url, store)


@pytest.fixture(scope="session")
def live_target():
    from api_targets import Target

    base = os.environ.get("UL_HOUSE_API_BASE_URL")
    if not base:
        pytest.skip("UL_HOUSE_API_BASE_URL is not set")
    keys = tuple(bytes.fromhex(k.strip()) for k in os.environ.get("UL_HOUSE_API_PUBLIC_KEYS", "").split(",") if k.strip())
    return Target("live", base.rstrip("/"), None, keys, os.environ.get("UL_HOUSE_API_EXPECTED_COMMIT") or None)


@pytest.fixture(scope="session", params=["fastapi", "fastapi_r2", "plain", "static", pytest.param("live", marks=pytest.mark.live)])
def target(request):
    return request.getfixturevalue(f"{request.param}_target")


@pytest.fixture
def http(target):
    import httpx

    with httpx.Client(
        base_url=target.base_url,
        follow_redirects=False,
        timeout=10,
        headers={"Accept-Encoding": "identity", "User-Agent": "ul-house-api-conformance"},
    ) as client:
        yield client
