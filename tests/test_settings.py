"""settings, catalog.toml validated (tracks round-trip and clamping/restriction band never widens)"""
import copy
import warnings

import pytest

from ul_house.settings import (
    CATALOG_PATH,
    Scope,
    ScopeError,
    UserSettings,
    clamp,
    load_scope,
)


@pytest.fixture(scope="module")
def scope():
    return load_scope()


@pytest.fixture
def raw(scope):
    return copy.deepcopy(scope.to_mapping())


def build(raw):
    return Scope.from_mapping(raw)


class TestShippedCatalog:
    def test_loads(self, scope):
        assert scope.cost.min == 42 and scope.cost.max == 98 and scope.cost.cosmetic == 99
        assert scope.rarity.include == ("UR",)
        assert scope.names.bypass == ("xeno", "sopho")

    def test_per_chain_overrides(self, scope):
        evo = scope.evolution
        assert (evo.policy_for("reforge"), evo.depth_for("reforge")) == ("catalog", 1)
        assert (evo.policy_for("enlightening"), evo.depth_for("enlightening")) == ("reference", 2)
        assert (evo.policy_for("awakening"), evo.depth_for("awakening")) == ("reference", 1)
        assert evo.max_depth == 2

    def test_is_package_file(self):
        assert CATALOG_PATH.name == "catalog.toml" and CATALOG_PATH.exists()


class TestStamp:
    def test_round_trip(self, scope):
        assert Scope.from_stamp(scope.stamp()) == scope
        assert build(scope.to_mapping()) == scope

    def test_fingerprint_stable_and_sensitive(self, scope, raw):
        assert scope.fingerprint() == load_scope().fingerprint()
        raw["cost"]["min"] = 43
        assert build(raw).fingerprint() != scope.fingerprint()

    def test_stale_stamp_fails(self, scope):
        stamp = scope.stamp().replace('"app_budget"', '"app_budjet"')
        with pytest.raises(ScopeError, match="unknown key"):
            Scope.from_stamp(stamp)


class TestStrictKeys:
    def test_unknown_top_level_table(self, raw):
        raw["crawlr"] = {}
        with pytest.raises(ScopeError, match=r"\[scope\] unknown key\(s\): crawlr"):
            build(raw)

    def test_typo_inside_table(self, raw):
        raw["crawl"]["request_intervall"] = raw["crawl"].pop("request_interval")
        with pytest.raises(ScopeError, match="crawl"):
            build(raw)

    def test_missing_key(self, raw):
        del raw["cost"]["cosmetic"]
        with pytest.raises(ScopeError, match="missing key"):
            build(raw)

    def test_unknown_chain(self, raw):
        raw["evolution"]["per_chain"]["rebirth"] = {"depth": 1}
        with pytest.raises(ScopeError, match="per_chain"):
            build(raw)

    def test_unknown_chain_option(self, raw):
        raw["evolution"]["per_chain"]["reforge"]["follow"] = True
        with pytest.raises(ScopeError, match="per_chain.reforge"):
            build(raw)

    @pytest.mark.parametrize("table, key, value", [
        ("cost", "min", "42"),
        ("cost", "max", True),
        ("crawl", "request_interval", "fast"),
        ("evolution", "override_filters", 1),
        ("rarity", "include", "UR"),
    ])
    def test_types(self, raw, table, key, value):
        raw[table][key] = value
        with pytest.raises(ScopeError, match=f"{table}.{key}"):
            build(raw)

    def test_bad_toml(self, tmp_path):
        path = tmp_path / "catalog.toml"
        path.write_text("[cost\nmin = 1")
        with pytest.raises(ScopeError):
            load_scope(path)


class TestCrossFieldRules:
    @pytest.mark.parametrize("cost", [
        {"min": 60, "max": 50, "cosmetic": 99},
        {"min": 42, "max": 99, "cosmetic": 99},
        {"min": -1, "max": 50, "cosmetic": 99},
    ])
    def test_cost_order(self, raw, cost):
        raw["cost"] = cost
        with pytest.raises(ScopeError, match="cost"):
            build(raw)

    def test_rarity_exists_in_list_pages(self, raw):
        raw["rarity"]["include"] = ["UR", "LR"]
        with pytest.raises(ScopeError, match="rarity"):
            build(raw)

    def test_rarity_not_empty(self, raw):
        raw["rarity"]["include"] = []
        with pytest.raises(ScopeError):
            build(raw)

    def test_correct_vocab(self, raw):
        raw["evolution"]["follow"] = ["reforge", "fusion"]
        with pytest.raises(ScopeError, match="follow"):
            build(raw)

    def test_per_chain_followed(self, raw):
        raw["evolution"]["follow"] = ["awakening"]
        with pytest.raises(ScopeError, match="not in evolution.follow"):
            build(raw)

    def test_negative_depth(self, raw):
        raw["evolution"]["depth"] = -1
        with pytest.raises(ScopeError, match="depth"):
            build(raw)

    def test_zero_depth_with_follow_warns(self, raw):
        raw["evolution"]["depth"] = 0
        del raw["evolution"]["per_chain"]
        with pytest.warns(UserWarning, match="depth is 0"):
            build(raw)

    def test_zero_depth_without_follow_quiet(self, raw):
        raw["evolution"].update(depth=0, follow=[])
        del raw["evolution"]["per_chain"]
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            build(raw)

    @pytest.mark.parametrize("key, value", [("direction", "sideways"), ("policy", "keep")])
    def test_evolution_vocab(self, raw, key, value):
        raw["evolution"][key] = value
        with pytest.raises(ScopeError, match=key):
            build(raw)

    @pytest.mark.parametrize("key, value", [("request_interval", 0), ("max_retries", -1), ("read_timeout", 0)])
    def test_crawl_bounds(self, raw, key, value):
        raw["crawl"][key] = value
        with pytest.raises(ScopeError):
            build(raw)

    def test_bypass_and_excluded_disjoint(self, raw):
        raw["names"]["excluded"].append("xeno")
        with pytest.raises(ScopeError, match="overlap"):
            build(raw)

    def test_tokens_lowercase(self, raw):
        raw["names"]["bypass"] = ["Xeno"]
        with pytest.raises(ScopeError, match="lowercase"):
            build(raw)


class TestNameMatching:
    def test_substring_and_case_insensitive(self, scope):
        assert scope.names.bypass_token("Jewel Bright Xenoblade") == "xeno"
        assert scope.names.excluded_token("[Awakening Ninoyu] Something") == "awakening ninoyu"
        assert scope.names.bypass_token("Blade of Ignis") is None

    def test_cost_band(self, scope):
        assert scope.cost.contains(42) and scope.cost.contains(98)
        assert not scope.cost.contains(41) and not scope.cost.contains(99)


class TestClamp:
    def test_default_is_scope(self, scope):
        result = clamp(UserSettings.from_scope(scope), scope)
        assert result.widened == ()
        assert result.settings == UserSettings.from_scope(scope)

    def test_narrowing_is_free(self, scope):
        wanted = UserSettings(60, 80, ("UR",), ("awakening ninoyu", "fatewoven"), "exclude")
        result = clamp(wanted, scope)
        assert result.widened == ()
        assert result.settings == wanted

    def test_widening_is_clamped_and_reported(self, scope):
        wanted = UserSettings(10, 99, ("UR", "SSR"), (), "catalog")
        result = clamp(wanted, scope)
        assert set(result.widened) == {"cost_min", "cost_max", "rarity_include", "names_excluded", "evolution_policy"}
        s = result.settings
        assert (s.cost_min, s.cost_max, s.rarity_include) == (42, 98, ("UR",))
        assert s.names_excluded == ("awakening ninoyu",)
        assert s.evolution_policy == "reference"

    def test_inverted_band_collapses_not_invert(self, scope):
        s = clamp(UserSettings(90, 50, ("UR",)), scope).settings
        assert s.cost_min <= s.cost_max
