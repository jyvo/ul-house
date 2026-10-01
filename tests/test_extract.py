from ul_house.config import BASE_URL, detail_path, detail_url
from ul_house.extract import (
    fetch_ability,
    fetch_data,
    fetch_name,
    fetch_reforge,
    fetch_skills,
    fetch_sp_evo,
    fetch_stats,
)


def test_detail_url():
    assert detail_path("1015655") == "/en/equip_detail/1015655.html"
    assert detail_url("1015655") == f"{BASE_URL}/en/equip_detail/1015655.html"


class TestName:
    def test_name(self, soups):
        assert fetch_name(soups["1015655"]) == "[surging sea] sea dragon's sword"


class TestData:
    def test_keys_are_spaced_and_casefolded(self, soups):
        """parser looks these up verbatim, so the exact spelling matters"""
        assert set(fetch_data(soups["1015655"])) == {
            "rarity",
            "gear type",
            "gear cost",
            "element",
            "infusion count",
            "max level",
        }

    def test_values(self, soups):
        data = fetch_data(soups["1015655"])
        assert data["rarity"] == "ur"
        assert data["gear type"] == "sword"
        assert data["gear cost"] == "44"

    def test_monsters_report_potential_not_infusion(self, soups):
        data = fetch_data(soups["1796604"])
        assert "hidden potential count" in data
        assert "infusion count" not in data

    def test_missing_table_is_none(self):
        from bs4 import BeautifulSoup

        assert fetch_data(BeautifulSoup("<html></html>", "lxml")) is None


class TestStats:
    def test_shape(self, soups):
        stats = fetch_stats(soups["1015655"])
        assert stats["atk"] == [("initial", 10111), ("max *1", 32741), ("max *2", 72097)]

    def test_blank_columns_are_skipped(self, soups):
        """a sword leaves def/mdef as '-'"""
        assert set(fetch_stats(soups["1015655"])) == {"atk", "matk"}

    def test_values_are_ints(self, soups):
        for values in fetch_stats(soups["1890424"]).values():
            assert all(isinstance(value, int) for _, value in values)


class TestAbility:
    def test_shape(self, soups):
        ability = fetch_ability(soups["1015655"])
        assert ability["uid"] == "2130"
        assert ability["name"] == "valiant blade"
        assert ability["effect"].startswith("target: 1 enemy.")

    def test_absent_ability_block(self, soups):
        assert fetch_ability(soups["1015157"]) is None


class TestSkills:
    def test_returns_heading_pairs(self, soups):
        """a list of pairs, not a mapping: headings repeat as 'skill #1' / 'skill #2'"""
        blocks = fetch_skills(soups["1500502"])
        assert [heading for heading, _ in blocks] == [
            "skill #1",
            "skill #2",
            "hidden potential",
            "restrictions",
        ]

    def test_proc_heading_and_pair(self, soups):
        ((heading, (name, effect)),) = fetch_skills(soups["1015655"])
        assert heading == "skill"
        assert name == "water dragon slayer xl"
        assert "[activation rate]" in effect

    def test_passive_has_own_heading(self, soups):
        headings = [heading for heading, _ in fetch_skills(soups["1796604"])]
        assert "passive skill" in headings
        assert "skill" in headings

    def test_hidden_potential_mapping(self, soups):
        potential = dict(fetch_skills(soups["1796604"]))["hidden potential"]
        assert set(potential) == {f"lv{n} effect" for n in range(1, 5)}

    def test_restrictions_exclude_inner_div(self, soups):
        """strictly the <dd> text"""
        restrictions = dict(fetch_skills(soups["1500502"]))["restrictions"]
        assert restrictions == ("[raging howl fatewoven] fermuraze or ninoyu with the same or higher cost of the base monster can be used as fodder.")
        assert "help section" not in restrictions

    def test_restrictions_leave_page_intact(self, soups):
        """extract <dd> text while shared soup still has inner div"""
        fetch_skills(soups["1500502"])
        assert soups["1500502"].select_one("div.caution_color_1") is not None

    def test_blocks_in_page_order(self, soups):
        headings = [heading for heading, _ in fetch_skills(soups["1796604"])]
        assert headings.index("skill") < headings.index("passive skill")


class TestReforge:
    def test_returns_entries_and_materials(self, soups):
        entries, materials = fetch_reforge(soups["1015655"])
        assert entries == [
            {
                "reforge info": {
                    "before": "sea dragon's sword",
                    "before_id": "1014667",
                    "after": "-",
                }
            }
        ]
        assert materials is None

    def test_absent_slot_is_a_dash(self, soups):
        entries, _ = fetch_reforge(soups["1015157"])
        assert entries[0]["reforge info"] == {"before": "-", "after": "-"}

    def test_materials_are_counted(self, soups):
        _, materials = fetch_reforge(soups["4434015"])
        assert materials["1104903"] == {"name": "emerald spirit orb", "quantity": 4}


class TestSpecialEvolution:
    def test_sides_arrive_as_separate_entries(self, soups):
        """parser has to merge these back into one Awakening"""
        entries, _ = fetch_sp_evo(soups["1015157"])
        assert [set(e["awakening info"]) for e in entries] == [
            {"before", "before_id"},
            {"after", "after_id"},
        ]

    def test_material_blocks(self, soups):
        _, materials = fetch_sp_evo(soups["1015157"])
        headings = [key for block in materials for key in block]
        assert headings == ["materials needed (gear)", "materials needed (items)"]

    def test_material_entry_is_name_and_quantity(self, soups):
        _, materials = fetch_sp_evo(soups["1015157"])
        items = materials[1]["materials needed (items)"]
        assert items["3077"] == ("ice beast horn", "1000")

    def test_enlightening_heading(self, soups):
        entries, _ = fetch_sp_evo(soups["4435013"])
        assert all("enlightening info" in entry for entry in entries)

    def test_absent_section(self, soups):
        assert fetch_sp_evo(soups["1015655"]) == (None, None)
