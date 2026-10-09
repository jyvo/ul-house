"""build/load.py: seed + Records -> DuckDB raw (no dbt)"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from catalog_fixtures import CATALOG
from fetch_fixtures import UIDS, cached_path
from ul_house import rows as R
from ul_house.build import load
from ul_house.parse import PARSER_VERSION

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "src" / "ul_house" / "build"

STARTED = "2026-10-05T00:00:00Z"
RUN_ID = "t20261005T000000Z-abc123"

SEED_COUNTS = {"page": 21, "listing": 10, "frontier": 14, "evo_link": 10, "asset": 8,
               "crawl_run": 1, "unlisted_release": 1}


def empty_records(**tables) -> R.Records:
    base = {name: [] for name in R.STAGING_TABLES}
    base.update(tables)
    parse_errors = base.pop("parse_errors", [])
    return R.Records(run_id=RUN_ID, parser_version=PARSER_VERSION, **base, parse_errors=parse_errors)


def equipment(uid="1000001", **over) -> R.StgEquipment:
    values = dict(source_uid=uid, source_hash="h" * 64, model_class="weapon", name="name", rarity="ur",
                  gear_type="sword", cost=40, element_id="fire", max_level=100)
    values.update(over)
    return R.StgEquipment(**values)


def raw_con():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    return con


def dump(con, table: str):
    return con.execute(f'SELECT * FROM raw."{table}"').fetchall()


def copy_seed(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "seed.sqlite"
    shutil.copyfile(src, dst)
    return dst


def edit(seed: Path, *statements: str) -> None:
    con = sqlite3.connect(seed)
    try:
        for statement in statements:
            con.execute(statement)
        con.commit()
    finally:
        con.close()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def duck_columns(con, table: str, schema: str = "raw") -> list[tuple[str, str, bool]]:
    return [(name, dtype, nullable == "YES") for name, dtype, nullable in con.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position", [schema, table]).fetchall()]


@pytest.fixture
def work(tmp_path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def seed_copy(mini_seed_path, tmp_path) -> Path:
    return copy_seed(mini_seed_path, tmp_path / "seed_copy")


def loaded(seed: Path, work: Path, **kw) -> load.LoadReport:
    return load.load_seed(seed, work, run_id=kw.pop("run_id", RUN_ID), started_at=kw.pop("started_at", STARTED), **kw)


def mart(work: Path):
    return duckdb.connect(str(work / "mart.duckdb"), read_only=True)


class TestTypeMaps:
    def test_sqlite_to_duckdb(self):
        assert load.SQLITE_TO_DUCKDB == {"INTEGER": "BIGINT", "TEXT": "VARCHAR", "REAL": "DOUBLE", "BLOB": "BLOB"}

    def test_record_to_duckdb(self):
        assert load.RECORD_TO_DUCKDB == {"TEXT": "VARCHAR", "INTEGER": "BIGINT"}

    def test_seed_table_list(self):
        assert load.SEED_TABLES == ("page", "asset", "listing", "frontier", "evo_link", "crawl_run",
                                    "unlisted_release", "meta")
        assert load.RAW_SCHEMA == "raw"

    def test_bytes_never_enter_duckdb(self):
        assert load.DROPPED_COLUMNS == {"page": ("html",), "asset": ("body",)}


class TestRecordTables:
    def test_every_records_table_is_one_raw_table_with_exact_columns(self):
        con = raw_con()
        records = empty_records()
        load.load_records(con, records)
        for table, _ in records.tables().items():
            record_type = R.RECORD_TYPES[table]
            expected = [(name, load.RECORD_TO_DUCKDB[sql_type], nullable)
                        for name, sql_type, nullable in R.columns(record_type)]
            got = [(name, dtype, nullable) for name, dtype, nullable in duck_columns(con, table)]
            assert [(n, t) for n, t, _ in got] == [(n, t) for n, t, _ in expected], table
            for (name, _, expected_nullable), (_, _, nullable) in zip(expected, got):
                if not expected_nullable:
                    assert not nullable, f"{table}.{name} should be NOT NULL"
            assert con.execute(f'SELECT count(*) FROM raw."{table}"').fetchone() == (0,), table

    def test_parse_errors_land_in_raw_parse_error(self):
        con = raw_con()
        errors = [R.ParseError("1000001", "h" * 64, PARSER_VERSION, "ValueError: boom", RUN_ID)]
        load.load_records(con, empty_records(parse_errors=errors))
        assert dump(con, "parse_error") == [("1000001", "h" * 64, PARSER_VERSION, "ValueError: boom", RUN_ID)]

    def test_counts_returned(self):
        con = raw_con()
        counts = load.load_records(con, empty_records(stg_equipment=[equipment("1"), equipment("2")]))
        assert counts["stg_equipment"] == 2
        assert counts["stg_stat"] == 0
        assert set(counts) >= set(R.STAGING_TABLES) | {"parse_error"}

    def test_row_order_is_preserved(self):
        con = raw_con()
        uids = ["9", "1", "5", "3"]
        load.load_records(con, empty_records(stg_equipment=[equipment(u) for u in uids]))
        assert [r[0] for r in dump(con, "stg_equipment")] == uids


TRICKY = [
    "plain",
    "",
    "   leading and trailing   ",
    "日本語のテキスト",
    "emoji \U0001F409 astral",
    "é decomposed",
    "é composed",
    "tab\there",
    "line\nbreak\r\nwindows",
    "unit\x1fseparator",
    "record\x1eseparator",
    "nul\x00inside",
    "quote \" and backslash \\ and 'single'",
    "ctl \x01\x02\x7f",
    "bidi ‮ mark",
    "zero​width",
    "NULL",
    "null",
]


class TestFidelity:
    def test_strings_survive_byte_for_byte(self):
        con = raw_con()
        records = empty_records(stg_proc=[R.StgProc(f"{i:07d}", text, "e" * 64, None, text) for i, text in enumerate(TRICKY)])
        load.load_records(con, records)
        got = dump(con, "stg_proc")
        assert [r[1] for r in got] == TRICKY
        assert [r[4] for r in got] == TRICKY

    def test_null_and_empty_string_stay_distinct(self):
        con = raw_con()
        records = empty_records(stg_monster=[R.StgMonster("1", None), R.StgMonster("2", ""), R.StgMonster("3", "NULL")])
        load.load_records(con, records)
        assert dump(con, "stg_monster") == [("1", None), ("2", ""), ("3", "NULL")]

    def test_nullable_target_column(self):
        con = raw_con()
        records = empty_records(stg_potential_level=[R.StgPotentialLevel("1", 1, None, "d"), R.StgPotentialLevel("1", 2, "", "d")])
        load.load_records(con, records)
        assert [r[2] for r in dump(con, "stg_potential_level")] == [None, ""]

    @pytest.mark.parametrize("value", [0, 1, -1, 2**31, 2**53 + 1, 2**63 - 1, -(2**63)])
    def test_integers_in_signed_64_bit_range(self, value):
        con = raw_con()
        load.load_records(con, empty_records(stg_stat=[R.StgStat("1", "atk", "initial", value, 1, 1)]))
        assert dump(con, "stg_stat")[0][3] == value
        assert isinstance(dump(con, "stg_stat")[0][3], int)

    def test_column_types_are_wide(self):
        con = raw_con()
        load.load_records(con, empty_records())
        types = {n: t for n, t, _ in duck_columns(con, "stg_stat")}
        assert types["value"] == "BIGINT"
        assert types["source_uid"] == "VARCHAR"

    @pytest.mark.parametrize("bad", [2**63, -(2**63) - 1, 2**64, 10**30])
    def test_out_of_range_integer_refused(self, bad):
        with pytest.raises(load.LoadError):
            load.load_records(raw_con(), empty_records(stg_stat=[R.StgStat("1", "atk", "initial", bad, 1, 1)]))

    @pytest.mark.parametrize("bad", [True, False])
    def test_bool_is_not_an_integer(self, bad):
        with pytest.raises(load.LoadError):
            load.load_records(raw_con(), empty_records(stg_stat=[R.StgStat("1", "atk", "initial", bad, 1, 1)]))

    @pytest.mark.parametrize("bad", [1.5, "7", b"7"])
    def test_wrong_python_type_refused(self, bad):
        with pytest.raises(load.LoadError):
            load.load_records(raw_con(), empty_records(stg_stat=[R.StgStat("1", "atk", "initial", bad, 1, 1)]))

    @pytest.mark.parametrize("bad", [1, b"bytes", 2.5, True])
    def test_non_string_in_text_column_refused(self, bad):
        with pytest.raises(load.LoadError):
            load.load_records(raw_con(), empty_records(stg_equipment=[equipment(name=bad)]))

    @pytest.mark.parametrize("bad", ["\ud800", "x\udfffy", "\udc00"])
    def test_lone_surrogate_refused(self, bad):
        with pytest.raises(load.LoadError):
            load.load_records(raw_con(), empty_records(stg_equipment=[equipment(name=bad)]))

    def test_none_in_non_nullable_column_names_table_and_column(self):
        with pytest.raises(load.LoadError, match=r"stg_equipment.*name|name.*stg_equipment"):
            load.load_records(raw_con(), empty_records(stg_equipment=[equipment(name=None)]))

    def test_none_in_non_nullable_integer_column(self):
        with pytest.raises(load.LoadError, match="stg_stat"):
            load.load_records(raw_con(), empty_records(stg_stat=[R.StgStat("1", "atk", "initial", None, 1, 1)]))

    def test_error_names_the_table_and_column_of_a_bad_type(self):
        with pytest.raises(load.LoadError, match=r"stg_stat"):
            load.load_records(raw_con(), empty_records(stg_stat=[R.StgStat("1", "atk", "initial", 2**63, 1, 1)]))

    def test_load_error_is_an_exception(self):
        assert issubclass(load.LoadError, Exception)

    def test_large_load_is_fast_and_exact(self):
        con = raw_con()
        rows = [R.StgStat(f"{i % 5000:07d}", f"label{i % 7}", f"tier{i // 7}", i * 1_000_003, 1, 1) for i in range(60_000)]
        load.load_records(con, empty_records(stg_stat=rows))
        assert con.execute("SELECT count(*), sum(value) FROM raw.stg_stat").fetchone() == (
            60_000, sum(r.value for r in rows))


class TestSeedTables:
    def test_counts_match_the_pinned_seed(self, mini_seed_path, seed_copy, work):
        report = loaded(seed_copy, work)
        with mart(work) as con:
            for table, expected in SEED_COUNTS.items():
                assert con.execute(f"SELECT count(*) FROM raw.{table}").fetchone() == (expected,), table
                assert report.counts[table] == expected
            assert con.execute("SELECT count(*) FROM raw.meta").fetchone()[0] == report.counts["meta"]

    def test_page_has_no_html_and_asset_has_body_bytes(self, seed_copy, work):
        loaded(seed_copy, work)
        from mini_seed import fake_png
        with mart(work) as con:
            page_cols = [c for c, _, _ in duck_columns(con, "page")]
            assert "html" not in page_cols and "html_sha256" in page_cols
            asset_cols = [c for c, _, _ in duck_columns(con, "asset")]
            assert "body" not in asset_cols and "body_bytes" in asset_cols
            got = dict(con.execute("SELECT item_id, body_bytes FROM raw.asset").fetchall())
            types = {c: t for c, t, _ in duck_columns(con, "asset")}
        assert got == {uid: len(fake_png(uid)) for uid in got} and len(got) == 8
        assert types["body_bytes"] == "BIGINT"

    def test_column_types_follow_the_declared_sqlite_types(self, seed_copy, work):
        loaded(seed_copy, work)
        lite = sqlite3.connect(seed_copy)
        with mart(work) as con:
            for table in load.SEED_TABLES:
                declared = lite.execute(f"PRAGMA table_xinfo({table})").fetchall()
                got = {name: (dtype, nullable) for name, dtype, nullable in duck_columns(con, table)}
                for _, name, decl, notnull, *_ in declared:
                    if name in load.DROPPED_COLUMNS.get(table, ()):
                        assert name not in got
                        continue
                    assert got[name][0] == load.SQLITE_TO_DUCKDB[decl.upper()], f"{table}.{name}"
                    if notnull:
                        assert got[name][1] is False, f"{table}.{name} should stay NOT NULL"
                names = [d[1] for d in declared if d[1] not in load.DROPPED_COLUMNS.get(table, ())]
                order = [c for c, _, _ in duck_columns(con, table) if c in names]
                assert order == names, table
        lite.close()

    def test_values_are_copied_exactly(self, seed_copy, work):
        loaded(seed_copy, work)
        lite = sqlite3.connect(seed_copy)
        with mart(work) as con:
            for table in load.SEED_TABLES:
                keep = [c for _, c, *_ in lite.execute(f"PRAGMA table_xinfo({table})").fetchall()
                        if c not in load.DROPPED_COLUMNS.get(table, ())]
                select = ", ".join(f'"{c}"' for c in keep)
                want = lite.execute(f"SELECT {select} FROM {table}").fetchall()
                got = con.execute(f"SELECT {select} FROM raw.{table}").fetchall()
                assert sorted(map(repr, got)) == sorted(map(repr, want)), table
        lite.close()

    def test_nulls_stay_null(self, seed_copy, work):
        loaded(seed_copy, work)
        with mart(work) as con:
            assert con.execute("SELECT count(*) FROM raw.page WHERE kind = 'detail' AND status = 404 "
                               "AND html_sha256 IS NULL").fetchone()[0] == 6
            assert con.execute("SELECT count(*) FROM raw.frontier WHERE parent_id IS NULL").fetchone()[0] == 7

    def test_schema_is_raw_and_nothing_else_is_created(self, seed_copy, work):
        loaded(seed_copy, work)
        with mart(work) as con:
            schemas = {r[0] for r in con.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE catalog_name = 'mart'").fetchall()}
            assert "raw" in schemas
            assert con.execute("SELECT count(*) FROM information_schema.tables "
                               "WHERE table_schema = 'main' AND table_catalog = 'mart'").fetchone() == (0,)

    def test_records_tables_sit_beside_the_seed_tables(self, seed_copy, work):
        loaded(seed_copy, work)
        with mart(work) as con:
            tables = {r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw'").fetchall()}
        assert tables == set(load.SEED_TABLES) | set(R.STAGING_TABLES) | {"parse_error"}


class TestSeedIsUntouched:
    def test_sha256_unchanged_and_no_sidecar_files(self, seed_copy, work):
        before = sha256_file(seed_copy)
        before_files = sorted(p.name for p in seed_copy.parent.iterdir())
        loaded(seed_copy, work)
        assert sha256_file(seed_copy) == before
        assert sorted(p.name for p in seed_copy.parent.iterdir()) == before_files
        assert not list(seed_copy.parent.glob("*-wal")) and not list(seed_copy.parent.glob("*-shm"))

    def test_read_only_even_on_a_read_only_file(self, seed_copy, work):
        seed_copy.chmod(0o444)
        try:
            loaded(seed_copy, work)
        finally:
            seed_copy.chmod(0o644)

    def test_the_session_seed_itself_is_not_modified(self, mini_seed_path, tmp_path):
        before = sha256_file(mini_seed_path)
        work = tmp_path / "w"
        work.mkdir()
        loaded(copy_seed(mini_seed_path, tmp_path / "c"), work)
        assert sha256_file(mini_seed_path) == before


class TestReadAndParse:
    def test_read_pages(self, seed_copy):
        pages = list(load.read_pages(seed_copy))
        assert [p[0] for p in pages] == sorted(UIDS)
        for item_id, sha, html in pages:
            assert isinstance(html, bytes)
            assert hashlib.sha256(html).hexdigest() == sha
            assert html == cached_path(item_id).read_bytes()

    def test_pages_without_bytes_are_not_read(self, seed_copy):
        assert len(list(load.read_pages(seed_copy))) == 8

    def test_parse_seed_equals_rows_of_the_cached_pages(self, seed_copy):
        records = load.parse_seed(seed_copy, run_id=RUN_ID)
        pages = [R.parse_page(uid, hashlib.sha256(cached_path(uid).read_bytes()).hexdigest(),
                              cached_path(uid).read_bytes()) for uid in sorted(UIDS)]
        assert records == R.rows(pages, run_id=RUN_ID, parser_version=PARSER_VERSION)
        assert records.run_id == RUN_ID and records.parser_version == PARSER_VERSION
        assert records.parse_errors == []

    def test_eight_equipment_records_seven_with_known_models(self, seed_copy):
        records = load.parse_seed(seed_copy, run_id=RUN_ID)
        by_uid = {r.source_uid: r for r in records.stg_equipment}
        assert len(by_uid) == 8 and set(by_uid) == set(UIDS)
        assert len(CATALOG) == 7
        for uid, model in CATALOG.items():
            r = by_uid[uid]
            assert (r.name, r.rarity, r.gear_type, r.cost, r.max_level) == (
                model.name, model.rarity, model.gear_type, model.cost, model.max_level), uid
            assert r.element_id == (model.element.id if model.element else "none"), uid
        assert "4434015" in by_uid and "4434015" not in CATALOG

    def test_source_hash_is_the_seeds_html_sha256(self, seed_copy):
        records = load.parse_seed(seed_copy, run_id=RUN_ID)
        lite = sqlite3.connect(seed_copy)
        want = dict(lite.execute("SELECT item_id, html_sha256 FROM page WHERE kind = 'detail' AND html IS NOT NULL"))
        lite.close()
        assert {r.source_uid: r.source_hash for r in records.stg_equipment} == want

    def test_a_corrupt_page_becomes_one_parse_error_and_the_rest_stay(self, seed_copy):
        garbage = b"\x00\x01 this is not an equipment page \xff"
        edit(seed_copy, f"UPDATE page SET html = x'{zlib.compress(garbage).hex()}', "
                        f"html_sha256 = '{hashlib.sha256(garbage).hexdigest()}' WHERE item_id = '1015157'")
        records = load.parse_seed(seed_copy, run_id=RUN_ID)
        assert [e.source_id for e in records.parse_errors] == ["1015157"]
        error = records.parse_errors[0]
        assert error.source_hash == hashlib.sha256(garbage).hexdigest()
        assert error.parser_version == PARSER_VERSION and error.run_id == RUN_ID and error.error
        assert {r.source_uid for r in records.stg_equipment} == set(UIDS) - {"1015157"}

    def test_sha_mismatch_is_a_load_error(self, seed_copy):
        edit(seed_copy, "UPDATE page SET html_sha256 = '" + "0" * 64 + "' WHERE item_id = '1015157'")
        with pytest.raises(load.LoadError):
            load.parse_seed(seed_copy, run_id=RUN_ID)

    def test_sha_mismatch_stops_load_seed(self, seed_copy, work):
        edit(seed_copy, "UPDATE page SET html_sha256 = '" + "0" * 64 + "' WHERE item_id = '1015157'")
        with pytest.raises(load.LoadError):
            loaded(seed_copy, work)

    def test_corrupt_page_reaches_raw_parse_error(self, seed_copy, work):
        garbage = b"garbage"
        edit(seed_copy, f"UPDATE page SET html = x'{zlib.compress(garbage).hex()}', "
                        f"html_sha256 = '{hashlib.sha256(garbage).hexdigest()}' WHERE item_id = '4434015'")
        report = loaded(seed_copy, work)
        assert report.parse_errors == 1
        with mart(work) as con:
            assert [r[0] for r in con.execute("SELECT source_id FROM raw.parse_error").fetchall()] == ["4434015"]
            assert con.execute("SELECT count(*) FROM raw.stg_equipment").fetchone() == (7,)


class TestLoadSeed:
    def test_records_land_in_raw_with_the_parsed_counts(self, seed_copy, work):
        report = loaded(seed_copy, work)
        records = load.parse_seed(seed_copy, run_id=RUN_ID)
        with mart(work) as con:
            for table, items in records.tables().items():
                assert con.execute(f'SELECT count(*) FROM raw."{table}"').fetchone() == (len(items),), table
        assert report.counts["stg_equipment"] == 8
        assert report.parse_errors == 0

    def test_report_fields(self, seed_copy, work):
        report = loaded(seed_copy, work)
        assert report.run_id == RUN_ID and report.started_at == STARTED
        assert report.seed_sha256 == sha256_file(seed_copy)
        assert report.crawl_run_id == 1
        assert report.parser_version == PARSER_VERSION
        assert isinstance(report, load.LoadReport)
        with pytest.raises(dataclasses.FrozenInstanceError):
            report.run_id = "x"

    def test_transform_run_json_is_written(self, seed_copy, work):
        report = loaded(seed_copy, work)
        data = json.loads((work / "transform_run.json").read_text())
        assert data == json.loads(json.dumps(report.to_mapping()))
        assert data["run_id"] == RUN_ID and data["crawl_run_id"] == 1

    def test_scratch_directory_is_removed(self, seed_copy, work):
        loaded(seed_copy, work)
        assert not (work / "load").exists()

    def test_generated_run_id_and_clock(self, seed_copy, work):
        report = load.load_seed(seed_copy, work, started_at=STARTED)
        assert re.fullmatch(r"t20261005T000000Z-[0-9a-f]{6}", report.run_id)
        report = load.load_seed(seed_copy, work, clock=lambda: "2026-11-01T01:02:03Z")
        assert report.started_at == "2026-11-01T01:02:03Z"
        assert report.run_id.startswith("t20261101T010203Z-")

    def test_new_run_id_shape_and_uniqueness(self):
        a, b = load.new_run_id(STARTED), load.new_run_id(STARTED)
        assert re.fullmatch(r"t20261005T000000Z-[0-9a-f]{6}", a)
        assert a != b

    def test_two_loads_give_identical_raw_content(self, seed_copy, tmp_path):
        content = []
        for name in ("one", "two"):
            work = tmp_path / name
            work.mkdir()
            loaded(seed_copy, work)
            with mart(work) as con:
                tables = sorted(r[0] for r in con.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw'").fetchall())
                content.append({t: sorted(map(repr, dump(con, t))) for t in tables})
        assert content[0] == content[1]

    def test_reload_in_the_same_work_dir_starts_from_a_fresh_mart(self, seed_copy, work):
        loaded(seed_copy, work)
        con = duckdb.connect(str(work / "mart.duckdb"))
        con.execute("CREATE TABLE leftover (x INTEGER)")
        con.execute("CREATE SCHEMA staging")
        con.close()
        loaded(seed_copy, work)
        with mart(work) as con:
            names = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
            schemas = {r[0] for r in con.execute("SELECT schema_name FROM information_schema.schemata").fetchall()}
        assert "leftover" not in names and "staging" not in schemas

    def test_history_is_left_alone(self, seed_copy, work):
        (work / "history").mkdir()
        history = work / "history" / "current.duckdb"
        con = duckdb.connect(str(history))
        con.execute("CREATE TABLE keep (x INTEGER)")
        con.close()
        before = sha256_file(history)
        loaded(seed_copy, work)
        assert sha256_file(history) == before

    def test_supplied_records_replace_the_parse(self, seed_copy, work):
        records = load.parse_seed(seed_copy, run_id=RUN_ID)
        patched = dataclasses.replace(records, stg_equipment=[
            dataclasses.replace(e, name="patched") if e.source_uid == "1015157" else e for e in records.stg_equipment])
        loaded(seed_copy, work, records=patched)
        with mart(work) as con:
            assert con.execute("SELECT name FROM raw.stg_equipment WHERE source_uid = '1015157'").fetchone() == ("patched",)

    def test_supplied_records_with_a_bad_value_are_refused(self, seed_copy, work):
        records = load.parse_seed(seed_copy, run_id=RUN_ID)
        bad = dataclasses.replace(records, stg_stat=[dataclasses.replace(records.stg_stat[0], value=True)])
        with pytest.raises(load.LoadError):
            loaded(seed_copy, work, records=bad)


class TestRefusals:
    def test_schema_version_other_than_1(self, seed_copy, work):
        edit(seed_copy, "UPDATE meta SET value = '2' WHERE key = 'seed.schema_version'")
        with pytest.raises(load.LoadError, match="schema"):
            loaded(seed_copy, work)

    def test_missing_schema_version(self, seed_copy, work):
        edit(seed_copy, "DELETE FROM meta WHERE key = 'seed.schema_version'")
        with pytest.raises(load.LoadError):
            loaded(seed_copy, work)

    @pytest.mark.parametrize("table", ["evo_link", "unlisted_release", "asset", "listing"])
    def test_missing_table(self, seed_copy, work, table):
        edit(seed_copy, f"DROP TABLE {table}")
        with pytest.raises(load.LoadError, match=table):
            loaded(seed_copy, work)

    def test_missing_last_complete_run(self, seed_copy, work):
        edit(seed_copy, "DELETE FROM meta WHERE key = 'crawl.last_complete_run'")
        with pytest.raises(load.LoadError, match="last_complete_run"):
            loaded(seed_copy, work)

    def test_active_run_is_refused_unless_partial_is_allowed(self, seed_copy, work):
        edit(seed_copy, "INSERT INTO meta (key, value) VALUES ('crawl.active_run', '2')")
        with pytest.raises(load.LoadError, match="active"):
            loaded(seed_copy, work)
        report = loaded(seed_copy, work, allow_partial=True)
        assert report.crawl_run_id == 1

    def test_unknown_declared_column_type(self, seed_copy, work):
        edit(seed_copy, "DROP TABLE unlisted_release",
             "CREATE TABLE unlisted_release (item_id TEXT PRIMARY KEY, name TEXT NOT NULL, first_run_id INTEGER, "
             "last_run_id INTEGER, runs NUMERIC)")
        with pytest.raises(load.LoadError, match="NUMERIC|runs"):
            loaded(seed_copy, work)

    def test_missing_file(self, tmp_path, work):
        with pytest.raises((load.LoadError, FileNotFoundError)):
            loaded(tmp_path / "absent.sqlite", work)

    def test_a_refused_load_leaves_the_seed_alone(self, seed_copy, work):
        edit(seed_copy, "DELETE FROM meta WHERE key = 'crawl.last_complete_run'")
        before = sha256_file(seed_copy)
        with pytest.raises(load.LoadError):
            loaded(seed_copy, work)
        assert sha256_file(seed_copy) == before


FORBIDDEN = ("ul_house.http", "ul_house.crawl", "requests", "dbt")


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


class TestImports:
    @pytest.mark.parametrize("name", ["__init__", "load", "dbt", "gate"])
    def test_no_network_or_dbt_imports(self, name):
        path = BUILD / f"{name}.py"
        assert path.exists(), path
        bad = {i for i in _imports(path)
               for f in FORBIDDEN if i == f or i.startswith(f + ".")}
        assert not bad, f"{name}.py imports {sorted(bad)}"

    def test_importing_the_package_loads_neither_dbt_nor_duckdb(self):
        code = ("import sys; import ul_house.build; "
                "bad = [m for m in ('dbt', 'duckdb', 'requests') if m in sys.modules]; "
                "sys.exit(1 if bad else 0)")
        assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0

    def test_importing_the_gate_loads_no_dbt(self):
        code = ("import sys; import ul_house.build.gate; "
                "bad = [m for m in ('dbt', 'requests') if m in sys.modules]; "
                "sys.exit(1 if bad else 0)")
        assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0

    def test_importing_the_dbt_wrapper_loads_no_dbt(self):
        code = ("import sys; import ul_house.build.dbt; "
                "bad = [m for m in ('dbt', 'requests') if m in sys.modules]; "
                "sys.exit(1 if bad else 0)")
        assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0
