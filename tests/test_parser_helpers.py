import pytest

from ul_house.models.equipment import EquipmentRef
from ul_house.parse import _equip_ref, _to_int, _lines, _parse_effects, _sections


class TestLines:
    def test_newline_separated(self):
        assert _lines("first.\nsecond.") == ["first.", "second."]

    def test_blank_lines_dropped(self):
        assert _lines("first.\n\n\nsecond.") == ["first.", "second."]

    def test_dash_separated(self):
        """monster effects > leading dash between each effect"""
        text = "-target: all enemies. hits them. -target: all allies. helps them."
        assert _lines(text) == [
            "target: all enemies. hits them.",
            "target: all allies. helps them.",
        ]

    def test_leading_dash(self):
        assert _lines("-only one.") == ["only one."]

    def test_no_split_on_hyphenated_text(self):
        assert _lines("eins - schwert") == ["eins - schwert"]

    def test_amplifier_split_from_effect(self):
        text = "ability power 400. physical damage (water). ability power boosted by 100 for each increase in skill level."
        assert _lines(text) == [
            "ability power 400. physical damage (water).",
            "ability power boosted by 100 for each increase in skill level.",
        ]

    def test_amplifier_standalone(self):
        text = "-ability power boosted by 10 for each increase in skill level."
        assert _lines(text) == ["ability power boosted by 10 for each increase in skill level."]

    def test_empty_text(self):
        assert _lines("") == []


class TestSections:
    TEXT = (
        "[effects]\nfirst effect.\nsecond effect.\n\n"
        "[activation]\nwhen hit.\n\n"
        "[activation rate]\nl\nrate scales with things."
    )

    def test_splits_on_markers(self):
        sections = _sections(self.TEXT)
        assert set(sections) == {"effects", "activation", "activation rate"}

    def test_effects_collected(self):
        assert _sections(self.TEXT)["effects"] == ["first effect.", "second effect."]

    def test_activation_rate_keeps_rate_and_scaling(self):
        assert _sections(self.TEXT)["activation rate"] == ["l", "rate scales with things."]

    def test_text_before_marker_is_an_effect(self):
        """procs without sections remain as a single effect line"""
        assert _sections("atk & matk of swords/axes +25%") == {
            "effects": ["atk & matk of swords/axes +25%"]
        }


class TestParseEffects:
    def test_target_split_out(self):
        (effect,) = _parse_effects("target: yourself. recovers 10 cost.")
        assert effect.target == "yourself"
        assert effect.description == "recovers 10 cost."

    def test_no_target_prefix(self):
        (effect,) = _parse_effects("increases all stats.")
        assert effect.target is None
        assert effect.description == "increases all stats."

    def test_only_leading_target_consumed(self):
        (effect,) = _parse_effects("target: 1 enemy. power 110. deals damage.")
        assert effect.target == "1 enemy"
        assert effect.description == "power 110. deals damage."

    def test_multiple_effects(self):
        assert len(_parse_effects("one.\ntwo.\nthree.")) == 3


class TestInt:
    @pytest.mark.parametrize(
        "value, expected",
        [("44", 44), ("1,000", 1000), (" 12 ", 12), (7, 7)],
    )
    def test_parses(self, value, expected):
        assert _to_int(value) == expected

    @pytest.mark.parametrize("value", [None, "", "-", "n/a"])
    def test_fallback(self, value):
        assert _to_int(value) == 0
        assert _to_int(value, 1) == 1


class TestEquipRef:
    def test_builds_ref(self):
        assert _equip_ref("oberon", "4434015") == EquipmentRef(uid="4434015", name="oberon")

    @pytest.mark.parametrize(
        "name, uid",
        [("-", "123"), (None, "123"), ("oberon", None), ("oberon", ""), ("", "123")],
    )
    def test_absent_slots_are_none(self, name, uid):
        """site renders an empty evolution slot as '-'"""
        assert _equip_ref(name, uid) is None
