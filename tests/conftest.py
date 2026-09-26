import pytest
import sample_catalog

CATALOG = {
    item.uid: item
    for item in (getattr(sample_catalog, name) for name in dir(sample_catalog) if name.isupper())
    if hasattr(item, "uid")
}


def catalog_ids() -> list[str]:
    return sorted(CATALOG)


def catalog_params():
    """(uid, expected model) for every catalogued page"""
    return [
        pytest.param(uid, CATALOG[uid], id=f"{CATALOG[uid].name}({uid})")
        for uid in catalog_ids()
    ]
