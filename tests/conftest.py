import pytest
from bs4 import BeautifulSoup

from fetch_fixtures import UIDS, ensure_cached, missing
from catalog_fixtures import CATALOG, SSR_REFORGE_UID, load_soup, catalog_ids, catalog_params


def pytest_configure(config):
    """top up local cache before collection (unless asked not to)"""
    if config.getoption("--no-fetch"):
        return
    if missing():
        ensure_cached()


@pytest.fixture(scope="session")
def soups() -> dict[str, BeautifulSoup]:
    """cached pages"""
    absent = missing()
    if absent:
        pytest.skip(
            f"{len(absent)} fixture page(s) not cached: {', '.join(absent)}. "
            "Run: python tests/fetch_fixtures.py"
        )
    return {uid: load_soup(uid) for uid in UIDS}


@pytest.fixture(scope="session")
def mini_seed_path(tmp_path_factory):
    mini_seed = pytest.importorskip("mini_seed")
    return mini_seed.build_mini_seed(tmp_path_factory.mktemp("mini") / "seed.sqlite")


@pytest.fixture
def offline_dbt(monkeypatch):
    """dbt children use this environment"""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
