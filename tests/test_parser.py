"""parse() against the models in sample_catalog"""
import dataclasses

import pytest

from ul_house.models.equipment import DefensiveGear, Monster, Weapon
from ul_house.parse import parse

from conftest import SSR_REFORGE_UID, catalog_params


@pytest.mark.parametrize("uid, expected", catalog_params())
def test_parses_to_catalogued_model(uid, expected, soups):
    assert parse(soups[uid], uid) == expected


@pytest.mark.parametrize("uid, expected", catalog_params())
def test_all_field_matches(uid, expected, soups):
    parsed = parse(soups[uid], uid)
    for field in dataclasses.fields(type(expected)):
        assert getattr(parsed, field.name) == getattr(expected, field.name), field.name


@pytest.mark.parametrize("uid, expected", catalog_params())
def test_correct_gear_type(uid, expected, soups):
    assert type(parse(soups[uid], uid)) is type(expected)


@pytest.mark.parametrize(
    "uid, model",
    [("1015655", Weapon), ("1890424", DefensiveGear), ("1796604", Monster)],
)
def test_gear_type_model_selection(uid, model, soups):
    assert isinstance(parse(soups[uid], uid), model)


def test_uid_from_the_caller(soups):
    """page carries no id of its own so parse() takes it as an arg"""
    assert parse(soups["1015655"], "some-other-id").uid == "some-other-id"


class TestWeapon:
    def test_weapon_carries_ability(self, soups):
        ability = parse(soups["1015655"], "1015655").weapon_ability
        assert ability.uid == "2130"
        assert ability.name == "valiant blade"

    def test_weapon_without_ability(self, soups):
        assert parse(soups["1015157"], "1015157").weapon_ability is None

    def test_scaling_split_from_effects(self, soups):
        skill = parse(soups["1015655"], "1015655").skill
        assert not any("scales" in effect.description for effect in skill.effect)
        assert len(skill.scaling) == 2

    def test_activation_captured(self, soups):
        activation = parse(soups["1015655"], "1015655").skill.activation
        assert activation.rate == "l"
        assert activation.condition == ("when a physical or magic attack ability is used.",)

    def test_proc_without_activation_block(self, soups):
        skill = parse(soups["1015157"], "1015157").skill
        assert skill.activation is None
        assert skill.scaling is None


class TestMonster:
    def test_single_skill_is_tuple(self, soups):
        assert len(parse(soups["1796604"], "1796604").skill) == 1

    def test_two_skills_captured(self, soups):
        skills = parse(soups["1500502"], "1500502").skill
        assert len(skills) == 2

    def test_passive_separated_from_skill(self, soups):
        monster = parse(soups["1796604"], "1796604")
        assert monster.passive.name == "eclipse's blessing"
        assert all(skill.name != monster.passive.name for skill in monster.skill)

    def test_monster_without_passive(self, soups):
        assert parse(soups["1500502"], "1500502").passive is None

    def test_hidden_potential_ordered(self, soups):
        potential = parse(soups["4435013"], "4435013").hidden_potential
        assert [unlock.level for unlock in potential.unlocks] == [1, 2, 3, 4, 5]
        assert potential.max_level == 5

    def test_hidden_potential_count(self, soups):
        assert parse(soups["1796604"], "1796604").hidden_potential_count == 4

    def test_restrictions_captured(self, soups):
        potential = parse(soups["1500502"], "1500502").hidden_potential
        assert potential.restrictions.startswith("[raging howl fatewoven]")

    def test_no_hidden_potential(self, soups):
        monster = parse(soups[SSR_REFORGE_UID], SSR_REFORGE_UID)
        assert monster.hidden_potential is None
        assert monster.hidden_potential_count == 0


class TestEvolution:
    def test_reforge_before_ref(self, soups):
        reforge = parse(soups["1015655"], "1015655").reforge
        assert reforge.before.uid == "1014667"
        assert reforge.after is None

    def test_empty_reforge_table(self, soups):
        """the table is always rendered, so an empty one means no reforge"""
        assert parse(soups["1015157"], "1015157").reforge is None

    def test_reforge_materials(self, soups):
        reforge = parse(soups[SSR_REFORGE_UID], SSR_REFORGE_UID).reforge
        assert reforge.after.uid == "4435013"
        assert {m.gear.uid: m.quantity for m in reforge.material} == {
            "1074071": 1,
            "1104903": 4,
        }

    def test_awakening_with_materials(self, soups):
        awakening = parse(soups["1015157"], "1015157").awakening
        assert awakening.before.uid == "1015065"
        assert awakening.after.uid == "1015615"
        assert len(awakening.gear_materials) == 5
        assert len(awakening.item_materials) == 5

    def test_awakening_without_materials(self, soups):
        awakening = parse(soups["4425111"], "4425111").awakening
        assert awakening.before.uid == "4424110"
        assert awakening.gear_materials is None

    def test_sep_sp_evo_types(self, soups):
        monster = parse(soups["4435013"], "4435013")
        assert monster.awakening is None
        assert monster.enlightening.after.uid == "4435353"

    def test_item_mat_quantities(self, soups):
        materials = parse(soups["1015157"], "1015157").awakening.item_materials
        assert {m.item.uid: m.quantity for m in materials}["3077"] == 1000


class TestStats:
    def test_tiers_normalised(self, soups):
        stats = {s.label: dict(s.values) for s in parse(soups["1015655"], "1015655").stats}
        assert stats["atk"] == {"initial": 10111, "max1": 32741, "max2": 72097}

    def test_blank_columns_dropped(self, soups):
        """a sword has no def/mdef, so those columns never become 'Stats'"""
        labels = {s.label for s in parse(soups["1015655"], "1015655").stats}
        assert labels == {"atk", "matk"}

    def test_values_are_ints(self, soups):
        for stat in parse(soups["1890424"], "1890424").stats:
            assert all(isinstance(value, int) for _, value in stat.values)

    def test_slotted_property(self, soups):
        """monsters may have selectable stat slots, gear does not"""
        monster = parse(soups["1796604"], "1796604")
        weapon = parse(soups["1015655"], "1015655")
        assert not any(stat.slotted for stat in monster.stats)
        assert all(stat.slotted for stat in weapon.stats)


def test_unknown_gear_type_returns_none():
    """page with no basic-info table"""
    from bs4 import BeautifulSoup

    assert parse(BeautifulSoup("<html><body></body></html>", "lxml"), "0") is None
