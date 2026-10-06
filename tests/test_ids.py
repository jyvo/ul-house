"""ids against golden vectors"""
import hashlib
import json
import random
import unicodedata
from pathlib import Path

import pytest

from ul_house import ids

GOLDEN = Path(__file__).parent / "golden"
HASH_IDS = GOLDEN / "hash_ids.json"
EFFECT_HASH = GOLDEN / "effect_hash.json"

US = "\x1f"
RS = "\x1e"
MASK = 0x7FFF_FFFF_FFFF_FFFF


def load_vectors(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)["vectors"]


HASH_VECTORS = load_vectors(HASH_IDS)
EFFECT_VECTORS = load_vectors(EFFECT_HASH)


def ref_canonical(*fields) -> bytes:
    parts = []
    for f in fields:
        if f is None:
            parts.append("")
        elif isinstance(f, int) and not isinstance(f, bool):
            parts.append(str(f))
        else:
            assert isinstance(f, str)
            parts.append(f)
    return US.join(parts).encode("utf-8")


def ref_hash_id(*fields) -> int:
    return int.from_bytes(hashlib.sha256(ref_canonical(*fields)).digest()[:8], "big") & MASK


def ref_effect_hash(effects, conditions=(), rate=None, scaling=()) -> str:
    lines = [f"e{US}{target or ''}{US}{description}" for target, description in effects]
    lines += [f"c{US}{c}" for c in conditions]
    if rate is not None:
        lines.append(f"r{US}{rate}")
    lines += [f"s{US}{s}" for s in scaling]
    return hashlib.sha256(RS.join(lines).encode("utf-8")).hexdigest()


def vector_params(vectors):
    return [pytest.param(v, id=v["name"]) for v in vectors]


# golden files
def test_golden_files_exist():
    assert HASH_IDS.is_file(), "tests/golden/hash_ids.json missing"
    assert EFFECT_HASH.is_file(), "tests/golden/effect_hash.json missing"


@pytest.mark.parametrize("vector", vector_params(HASH_VECTORS))
def test_golden_vectors(vector):
    fields = vector["fields"]
    assert isinstance(vector["expected"], int) and not isinstance(vector["expected"], bool)
    assert ids.hash_id(*fields) == vector["expected"]
    assert ids.canonical_key(*fields).hex() == vector["canonical_hex"]


@pytest.mark.parametrize("vector", vector_params(HASH_VECTORS))
def test_golden_vectors_match_reference(vector):
    """golden file and ids.py agree with ref implementation"""
    assert ref_hash_id(*vector["fields"]) == vector["expected"]
    assert ref_canonical(*vector["fields"]).hex() == vector["canonical_hex"]


def test_golden_file_coverage():
    assert len(HASH_VECTORS) >= 20
    names = [v["name"] for v in HASH_VECTORS]
    assert len(names) == len(set(names)), "duplicate vector names"
    kinds = {v["id"] for v in HASH_VECTORS}
    assert {
        "proc_family.family_id",
        "proc.proc_id",
        "skill_effect.effect_id",
        "passive_skill.passive_id",
        "monster_skill.skill_id",
    } <= kinds
    all_fields = [f for v in HASH_VECTORS for f in v["fields"]]
    assert "" in all_fields
    assert None in all_fields
    assert any(isinstance(f, str) and any(ord(c) > 0x7F for c in f) for f in all_fields), "non-ASCII"
    assert any(isinstance(f, str) and any(ord(c) > 0xFFFF for c in f) for f in all_fields), "astral"
    assert any(isinstance(f, int) for f in all_fields)


def test_golden_ids_above_2_53_stay_exact():
    big = [v["expected"] for v in HASH_VECTORS if v["expected"] > 2**53]
    assert big, "expected the golden file to contain ids above 2**53"
    assert all(isinstance(b, int) for b in big)


# testing vals
@pytest.mark.parametrize(
    "fields, expected",
    [
        pytest.param(("",), 7183457195969485844, id="empty_string"),
        pytest.param((None,), 7183457195969485844, id="none"),
        pytest.param(("yourself", "recovers 10 cost."), 904559173316423849, id="effect_yourself"),
        pytest.param(("1500502", 2), 4566135229701037697, id="skill_1500502_2"),
    ],
)
def test_independent_vals(fields, expected):
    assert ids.hash_id(*fields) == expected


# semantics
def test_63_bit_range():
    for v in HASH_VECTORS:
        assert 0 <= ids.hash_id(*v["fields"]) < 2**63
    rng = random.Random(20261004)
    alphabet = "abcXYZ019 -:.é日🐉"
    for _ in range(1000):
        n = rng.randint(1, 4)
        fields = [
            rng.choice(
                [
                    None,
                    rng.randint(-(2**40), 2**40),
                    "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12))),
                ]
            )
            for _ in range(n)
        ]
        value = ids.hash_id(*fields)
        assert 0 <= value < 2**63
        assert value == ref_hash_id(*fields)


def test_none_is_empty():
    assert ids.hash_id(None) == ids.hash_id("")
    assert ids.hash_id(None, "x") == ids.hash_id("", "x")
    assert ids.hash_id("x", None) == ids.hash_id("x", "")


def test_int_renders_decimal():
    assert ids.hash_id("a", 2) == ids.hash_id("a", "2")
    assert ids.hash_id(-1) == ids.hash_id("-1")
    assert ids.hash_id(0) == ids.hash_id("0")


def test_order_and_arity_matter():
    assert ids.hash_id("a", "b") != ids.hash_id("b", "a")
    assert ids.hash_id("a") != ids.hash_id("a", "")


def test_no_unicode_normalisation():
    nfc = "é"
    nfd = "é"
    assert unicodedata.normalize("NFC", nfd) == nfc
    assert ids.hash_id(nfc) != ids.hash_id(nfd)


def test_no_case_folding():
    assert ids.hash_id("Abc") != ids.hash_id("abc")


@pytest.mark.parametrize(
    "args, exc",
    [
        pytest.param((True,), TypeError, id="bool"),
        pytest.param((False,), TypeError, id="false"),
        pytest.param((1.0,), TypeError, id="float"),
        pytest.param((b"x",), TypeError, id="bytes"),
        pytest.param((), TypeError, id="no_fields"),
        pytest.param(("a", ["b"]), TypeError, id="list"),
        pytest.param(("a\x1fb",), ValueError, id="separator_in_text"),
    ],
)
def test_rejects(args, exc):
    with pytest.raises(exc):
        ids.hash_id(*args)
    with pytest.raises(exc):
        ids.canonical_key(*args)


def test_separator_constants():
    assert ids.SEPARATOR == US
    assert ids.RECORD_SEPARATOR == RS
    assert ids.ID_MASK == MASK


# helpers
NAMED = {
    "proc_family.family_id": ids.family_id,
    "proc.proc_id": ids.proc_id,
    "skill_effect.effect_id": ids.effect_id,
    "passive_skill.passive_id": ids.passive_id,
    "monster_skill.skill_id": ids.skill_id,
}


@pytest.mark.parametrize("vector", vector_params([v for v in HASH_VECTORS if v["id"] in NAMED]))
def test_named_helpers_match_golden(vector):
    assert NAMED[vector["id"]](*vector["fields"]) == vector["expected"]


def test_named_helpers():
    assert ids.family_id("dragon slayer") == ids.hash_id("dragon slayer")
    assert ids.proc_id("raw name", "abc") == ids.hash_id("raw name", "abc")
    assert ids.effect_id("yourself", "x.") == ids.hash_id("yourself", "x.")
    assert ids.effect_id(None, "x.") == ids.hash_id(None, "x.") == ids.hash_id("", "x.")
    assert ids.passive_id("p", "abc") == ids.hash_id("p", "abc")
    assert ids.skill_id("1500502", 2) == ids.hash_id("1500502", 2)
    assert ids.skill_id("1500502", 1) != ids.skill_id("1500502", 2)


# hash
def call_effect_hash(v):
    return ids.effect_hash(
        [tuple(e) for e in v["effects"]], v["conditions"], v["rate"], v["scaling"]
    )


def test_effect_hash_file_coverage():
    names = {v["name"] for v in EFFECT_VECTORS}
    assert len(EFFECT_VECTORS) >= 7
    assert {
        "proc_water_dragon_slayer_xl",
        "proc_mastery_over_dark_xl",
        "proc_patriot_sword",
        "passive_eclipses_blessing",
        "passive_eins_schwert",
        "empty",
        "rate_only",
    } <= names


@pytest.mark.parametrize("vector", vector_params(EFFECT_VECTORS))
def test_effect_hash_vectors(vector):
    assert call_effect_hash(vector) == vector["expected"]
    assert ref_effect_hash(
        vector["effects"], vector["conditions"], vector["rate"], vector["scaling"]
    ) == vector["expected"]


def test_effect_hash_empty_is_sha256_of_nothing():
    assert ids.effect_hash([]) == hashlib.sha256(b"").hexdigest()
    assert ids.effect_hash([]) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_effect_hash_shape_and_rejects():
    h = ids.effect_hash([(None, "x.")], ["when hit."], "l", ["scales."])
    assert len(h) == 64 and h == h.lower() and all(c in "0123456789abcdef" for c in h)
    assert ids.effect_hash([], rate="") != ids.effect_hash([], rate=None)
    for bad in ("a\x1eb", "a\x1fb"):
        with pytest.raises(ValueError):
            ids.effect_hash([(None, bad)])
        with pytest.raises(ValueError):
            ids.effect_hash([(bad, "x.")])
        with pytest.raises(ValueError):
            ids.effect_hash([], conditions=[bad])
        with pytest.raises(ValueError):
            ids.effect_hash([], rate=bad)
        with pytest.raises(ValueError):
            ids.effect_hash([], scaling=[bad])


def test_effect_hash_target_none_equals_empty():
    assert ids.effect_hash([(None, "x.")]) == ids.effect_hash([("", "x.")])


def test_effect_hash_sensitivity():
    base = ids.effect_hash([(None, "a.")], ["c."], "l", ["s."])
    assert ids.effect_hash([(None, "a.")], ["c."], "m", ["s."]) != base
    assert ids.effect_hash([(None, "a.")], ["d."], "l", ["s."]) != base
    assert ids.effect_hash([(None, "a.")], ["c."], "l", ["t."]) != base
    assert ids.effect_hash([(None, "a."), (None, "b.")]) != ids.effect_hash([(None, "b."), (None, "a.")])


# text helpers
def test_id_text_round_trip():
    values = [v["expected"] for v in HASH_VECTORS] + [0, 1, 2**63 - 1]
    for value in values:
        text = ids.id_to_text(value)
        assert text == str(value)
        assert ids.id_from_text(text) == value


def test_id_to_text_examples():
    assert ids.id_to_text(0) == "0"
    assert ids.id_to_text(2**63 - 1) == "9223372036854775807"


@pytest.mark.parametrize(
    "text",
    ["-1", "-7712093318843", "01", " 1", "1 ", "+1", "1.0", "", "9223372036854775808", "abc", "1_0"],
)
def test_id_from_text_rejects(text):
    with pytest.raises(ValueError):
        ids.id_from_text(text)


def test_id_from_text_accepts_zero_and_max():
    assert ids.id_from_text("0") == 0
    assert ids.id_from_text("9223372036854775807") == 2**63 - 1


@pytest.mark.parametrize(
    "value, exc",
    [
        pytest.param(True, TypeError, id="bool"),
        pytest.param("1", TypeError, id="str"),
        pytest.param(1.0, TypeError, id="float"),
        pytest.param(-1, ValueError, id="negative"),
        pytest.param(2**63, ValueError, id="too_big"),
    ],
)
def test_id_to_text_rejects(value, exc):
    with pytest.raises(exc):
        ids.id_to_text(value)
