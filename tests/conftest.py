import pytest
from bs4 import BeautifulSoup

import sample_catalog
from fetch_fixtures import UIDS, cached_path, ensure_cached, missing

CATALOG = {
    item.uid: item
    for item in (getattr(sample_catalog, name) for name in dir(sample_catalog) if name.isupper())
    if hasattr(item, "uid")
}

# page with no catalog entry (for paths that do not cover)
SSR_REFORGE_UID = "4434015"


def pytest_addoption(parser):
    parser.addoption(
        "--no-fetch",
        action="store_true",
        default=False,
        help="do not download missing fixture pages; skip the tests that need them",
    )


def pytest_configure(config):
    """top up local cache before collection (unless asked not to)"""
    if config.getoption("--no-fetch"):
        return
    if missing():
        ensure_cached()


def load_soup(uid: str) -> BeautifulSoup:
    return BeautifulSoup(cached_path(uid).read_text(), "lxml")


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


def catalog_ids() -> list[str]:
    return sorted(CATALOG)


def catalog_params():
    """(uid, expected model) for every catalogued page"""
    return [
        pytest.param(uid, CATALOG[uid], id=f"{CATALOG[uid].name}({uid})")
        for uid in catalog_ids()
    ]
