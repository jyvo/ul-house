"""scope rules against catalog.toml"""
import pytest

from ul_house.crawl.discover import ListRow
from ul_house.select import Decision, decide, select
from ul_house.settings import load_scope


@pytest.fixture(scope="module")
def scope():
    return load_scope()


def row(name="Plain Blade", cost=50, rarity="UR", item_id="1"):
    return ListRow(item_id, name, "weapon", rarity, "1", "2", cost, "/en/equip_list/1_5.html")


@pytest.mark.parametrize("r, expected", [
    (row(cost=42), Decision(True, "cost_band")),
    (row(cost=98), Decision(True, "cost_band")),
    (row(cost=41), Decision(False, "cost")),
    (row(cost=99), Decision(False, "cosmetic")),
    (row(rarity="SSR"), Decision(False, "rarity")),
    (row(name="Jewel Bright Xenoblade", cost=20, rarity="SSR"), Decision(True, "name_token")),
    (row(name="Sophos Tome", cost=5), Decision(True, "name_token")),
    (row(name="[Awakening Ninoyu] Charm", cost=50), Decision(False, "excluded_name")),
])
def test_rules(scope, r, expected):
    assert decide(r, scope) == expected


def test_cosmetic_beats_bypass(scope):
    assert decide(row(name="Xeno Relic", cost=99), scope) == Decision(False, "cosmetic")


def test_select_keeps_sorted_and_counts(scope):
    rows = [row(item_id="3", cost=50), row(item_id="1", cost=10), row(item_id="2", name="xeno", cost=10)]
    selection = select(rows, scope)
    assert [r.item_id for r, _ in selection.kept] == ["2", "3"]
    assert selection.reasons == {"cost_band": 1, "cost": 1, "name_token": 1}


def test_entries_are_depth_zero_catalog(scope):
    (entry,) = select([row(item_id="5")], scope).entries()
    assert (entry.item_id, entry.source, entry.entry_kind, entry.keep_reason, entry.depth, entry.parent_id) == ("5", "list", "catalog", "cost_band", 0, None)
