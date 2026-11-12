import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACCEPTED = ["xs", "xxs", "s", "m", "l", "xl", "xxl", "3xl", "xl+", "2m", "XS"]
REJECTED = ["x", "xx", "12xl", "xl++", "", "small"]


def _pattern():
    cfg = yaml.safe_load((ROOT / "analytics/dbt_project.yml").read_text())
    return cfg["vars"]["proc_size_pattern"]


def _candidate():
    sql = (ROOT / "analytics/tests/policy/gate__unknown_vocabulary__proc_size.sql").read_text()
    m = re.search(r"regexp_matches\(p\.raw_name, '(.*?)'\)", sql)
    assert m
    return m.group(1).replace("(?i)", "", 1)


@pytest.mark.parametrize("value", ACCEPTED)
def test_size_pattern_accepts(value):
    assert re.fullmatch(_pattern(), value, re.IGNORECASE)


@pytest.mark.parametrize("value", REJECTED)
def test_size_pattern_rejects(value):
    assert not re.fullmatch(_pattern(), value, re.IGNORECASE)


@pytest.mark.parametrize("value", ["12xl", "xl++"])
def test_gate_candidate_flags_junk(value):
    assert re.search(_candidate(), f"water dragon slayer {value}", re.IGNORECASE)


@pytest.mark.parametrize("name", ["water dragon slayer small", "water dragon slayer will",
                                  "water dragon slayer x", "water dragon slayer xx",
                                  "water dragon slayer helm", "water dragon slayer ", "slayer"])
def test_gate_candidate_ignores_words_not_accepted_no_warning(name):
    assert not re.search(_candidate(), name, re.IGNORECASE)
