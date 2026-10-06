import pytest
from bs4 import BeautifulSoup

import sample_catalog
from fetch_fixtures import cached_path

CATALOG = {
    item.uid: item
    for item in (getattr(sample_catalog, name) for name in dir(sample_catalog) if name.isupper())
    if hasattr(item, "uid")
}

# page with no catalog entry (for paths that do not cover)
SSR_REFORGE_UID = "4434015"


def load_soup(uid: str) -> BeautifulSoup:
    return BeautifulSoup(cached_path(uid).read_text(), "lxml")


def catalog_ids() -> list[str]:
    return sorted(CATALOG)


def catalog_params():
    """(uid, expected model) for every catalogued page"""
    return [
        pytest.param(uid, CATALOG[uid], id=f"{CATALOG[uid].name}({uid})")
        for uid in catalog_ids()
    ]
