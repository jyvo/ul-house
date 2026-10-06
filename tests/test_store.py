import hashlib
import itertools
import re
import sqlite3
import zlib
from dataclasses import dataclass

import pytest

from ul_house.seed.store import SeedSchemaError, Store, Validators, utc_now


def ticking_clock():
    counter = itertools.count()
    return lambda: f"t{next(counter):03d}"


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "seed.sqlite", clock=ticking_clock()) as s:
        yield s


DETAIL = "/en/equip_detail/1015655.html"
LIST = "/en/equip_list/1_5.html"


def put_detail(store, body=b"<html>one</html>", etag='"a"', status=200):
    return store.record_response(DETAIL, "detail", status, body, etag, "Mon, 01 Jan 2026 00:00:00 GMT",
                                 "text/html", item_id="1015655")


class TestOpen:
    def test_wal(self, store):
        assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_reopening(self, tmp_path):
        Store.open(tmp_path / "s.sqlite").close()
        Store.open(tmp_path / "s.sqlite").close()

    def test_foreign_keys_enforced(self, store):
        assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


class TestPages:
    def test_round_trip_compressed(self, store):
        body = b"<html>" + b"row " * 5000 + b"</html>"
        store.record_response(LIST, "list", 200, body, '"e"', None, "text/html")
        raw = store.conn.execute("SELECT html FROM page WHERE url = ?", (LIST,)).fetchone()[0]
        assert len(raw) < len(body) / 10
        assert zlib.decompress(raw) == body
        assert store.html(LIST) == body.decode()

    def test_first_200(self, store):
        assert put_detail(store) is True
        assert store.page(DETAIL)["changed_at"] == store.page(DETAIL)["fetched_at"]

    def test_identical_200_not_a_change(self, store):
        put_detail(store)
        first = store.page(DETAIL)
        assert put_detail(store) is False
        second = store.page(DETAIL)
        assert second["changed_at"] == first["changed_at"]
        assert second["fetched_at"] != first["fetched_at"]

    def test_different_200_is_change(self, store):
        put_detail(store)
        assert put_detail(store, body=b"<html>two</html>") is True
        assert store.html(DETAIL) == "<html>two</html>"

    def test_304_keeps_html(self, store):
        # also refreshes fetched_at
        put_detail(store)
        before = store.page(DETAIL)
        assert store.record_response(DETAIL, "detail", 304, item_id="1015655") is False
        after = store.page(DETAIL)
        assert store.html(DETAIL) == "<html>one</html>"
        assert after["status"] == 304
        assert after["fetched_at"] != before["fetched_at"]
        assert after["changed_at"] == before["changed_at"]
        assert (after["etag"], after["last_modified"]) == (before["etag"], before["last_modified"])

    def test_304_may_refresh_validators(self, store):
        put_detail(store)
        store.record_response(DETAIL, "detail", 304, etag='"b"', item_id="1015655")
        assert store.validators(DETAIL).etag == '"b"'

    def test_304_error_without_stored_html(self, store):
        with pytest.raises(ValueError, match="no stored html"):
            store.record_response(DETAIL, "detail", 304, item_id="1015655")

    def test_error_status(self, store):
        # keeps page good
        put_detail(store)
        store.record_response(DETAIL, "detail", 503, item_id="1015655")
        assert store.page(DETAIL)["status"] == 503
        assert store.html(DETAIL) == "<html>one</html>"
        assert store.validators(DETAIL).etag == '"a"'

    def test_error_status_on_new_url(self, store):
        # stores no html
        store.record_response(DETAIL, "detail", 404, item_id="1015655")
        assert store.html(DETAIL) is None
        assert store.validators(DETAIL) is None

    def test_200_reqs_body(self, store):
        with pytest.raises(ValueError):
            store.record_response(DETAIL, "detail", 200, None, item_id="1015655")

    def test_detail_pages_req_item_id(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store.record_response(DETAIL, "detail", 200, b"x")

    def test_list_pages_does_not_carry_item_id(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store.record_response(LIST, "list", 200, b"x", item_id="1")

    def test_unknown_kind(self, store):
        with pytest.raises(ValueError):
            store.record_response(LIST, "index", 200, b"x")


class TestValidators:
    def test_headers(self):
        assert Validators('"a"', "Mon").headers() == {"If-None-Match": '"a"', "If-Modified-Since": "Mon"}
        assert Validators(None, None).headers() == {}

    def test_read_back(self, store):
        put_detail(store)
        assert store.validators(DETAIL) == Validators('"a"', "Mon, 01 Jan 2026 00:00:00 GMT")


@dataclass
class Row:
    item_id: str
    source_url: str = LIST
    name: str = "Blade"
    grp: str = "weapon"
    rarity: str = "UR"
    gear_type: str | None = "1"
    element: str = "2"
    cost: int = 50


class TestListing:
    def test_needs_source_page_first(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store.put_listing([Row("1")])

    def test_upsert(self, store):
        store.record_response(LIST, "list", 200, b"x")
        store.put_listing([Row("1"), Row("2", gear_type=None, grp="monster")])
        store.put_listing([Row("1", cost=60)])
        assert store.listing("1")["cost"] == 60
        assert store.listing("2")["gear_type"] is None
        assert [r["item_id"] for r in store.listing_all()] == ["1", "2"]


@dataclass
class Link:
    kind: str
    side: str
    target_id: str
    target_name: str = "x"


class TestLinks:
    def test_replace_per_source(self, store):
        store.put_links("A", [Link("reforge", "before", "B"), Link("awakening", "after", "C")])
        store.put_links("A", [Link("reforge", "before", "D")])
        assert [(r["kind"], r["target_id"]) for r in store.links_from(["A"])] == [("reforge", "D")]

    def test_lookup_both_ways(self, store):
        store.put_links("A", [Link("reforge", "before", "B")])
        store.put_links("C", [Link("awakening", "before", "B")])
        assert {r["source_id"] for r in store.links_to(["B"])} == {"A", "C"}
        assert store.links_from([]) == [] and store.links_to([]) == []


class TestMetaAndTransactions:
    def test_meta(self, store):
        assert store.get_meta("k", "d") == "d"
        store.set_meta("k", "1")
        store.set_meta("k", "2")
        assert store.get_meta("k") == "2"

    def test_rollback_on_error(self, store):
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.set_meta("k", "v")
                raise RuntimeError
        assert store.get_meta("k") is None

    def test_nested_transactions_join_the_outer(self, store):
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.put_links("A", [Link("reforge", "before", "B")])  # opens its own, nested
                raise RuntimeError
        assert store.links_from(["A"]) == []


class TestSavepoints:
    def test_caught_inner_failure_keeps_outer_writes_only(self, store):
        with store.transaction():
            store.set_meta("outer", "1")
            try:
                with store.transaction():
                    store.set_meta("inner", "1")
                    raise RuntimeError
            except RuntimeError:
                pass
            store.set_meta("after", "1")
        assert (store.get_meta("outer"), store.get_meta("inner"), store.get_meta("after")) == ("1", None, "1")

    def test_successful_inner_block_commits_with_the_outer(self, store):
        with store.transaction():
            with store.transaction():
                store.set_meta("inner", "1")
        assert store.get_meta("inner") == "1"

    def test_savepoints_nest_more_than_one_level(self, store):
        with store.transaction():
            with store.transaction():
                store.set_meta("a", "1")
                try:
                    with store.transaction():
                        store.set_meta("b", "1")
                        raise RuntimeError
                except RuntimeError:
                    pass
        assert (store.get_meta("a"), store.get_meta("b")) == ("1", None)

    def test_failed_commit_rolls_back_and_the_store_stays_usable(self, store):
        """a deferred foreign-key violation only surfaces at COMMIT"""
        with pytest.raises(sqlite3.IntegrityError):
            with store.transaction():
                store.conn.execute("PRAGMA defer_foreign_keys = ON")
                store.put_listing([Row("1", source_url="/never/stored.html")])
        assert not store.conn.in_transaction
        assert store.listing("1") is None
        with store.transaction():
            store.set_meta("k", "v")
        assert store.get_meta("k") == "v"


class TestThreads:
    def test_open_on_the_thread_that_uses_it(self, tmp_path):
        import threading

        path = tmp_path / "seed.sqlite"
        result = []

        def crawl_thread():
            with Store.open(path) as own:
                own.set_meta("k", "v")
                result.append(own.get_meta("k"))

        worker = threading.Thread(target=crawl_thread)
        worker.start()
        worker.join()
        assert result == ["v"]


def start(store, trigger="manual", shard_index=None):
    return store.start_run(trigger=trigger, code_commit="c", catalog_version="v", catalog_commit="k",
                           shard_index=shard_index)


def columns(store, table):
    return {row["name"] for row in store.conn.execute(f"PRAGMA table_info({table})")}


class TestSchema:
    def test_tables_and_columns(self, store):
        tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"asset", "crawl_run", "unlisted_release"} <= tables
        assert {"run_id", "changed_run_id"} <= columns(store, "page")
        assert "decided_run_id" in columns(store, "frontier")
        assert {"code_dirty", "new_release_skipped"} <= columns(store, "crawl_run")
        assert store.get_meta("seed.schema_version") == "1"

    def test_asset_kind_is_the_full_vocabulary(self, store):
        for kind in ("equipment", "item", "ability"):
            store.conn.execute("INSERT INTO asset (url, item_id, kind) VALUES (?, '1', ?)", (f"/{kind}.png", kind))
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("INSERT INTO asset (url, item_id, kind) VALUES ('/x.png', '1', 'weapon')")

    def test_utc_now_format(self):
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", utc_now())

    def test_old_seed_refused(self, tmp_path):
        path = tmp_path / "old.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE page (url TEXT PRIMARY KEY, kind TEXT NOT NULL, item_id TEXT, html BLOB, "
            "html_sha256 TEXT, etag TEXT, last_modified TEXT, status INTEGER NOT NULL, content_type TEXT, "
            "fetched_at TEXT NOT NULL, changed_at TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(SeedSchemaError):
            Store.open(path)

    def test_other_schema_version_refused(self, tmp_path):
        path = tmp_path / "s.sqlite"
        with Store.open(path) as s:
            s.set_meta("seed.schema_version", "3")
        with pytest.raises(SeedSchemaError):
            Store.open(path)


class TestRuns:
    def test_start_and_read(self, store):
        assert start(store) == 1
        assert start(store, trigger="schedule", shard_index=3) == 2
        row = store.run(1)
        assert row["status"] == "running" and row["started_at"] == "t000" and row["ended_at"] is None
        assert row["code_dirty"] == 0 and row["new_release_skipped"] == 0
        assert all(row[c] == 0 for c in ("pages_fetched", "icons_changed", "shard_items"))
        assert [r["run_id"] for r in store.runs()] == [1, 2]
        assert store.run(2)["shard_index"] == 3

    def test_code_dirty(self, store):
        run = store.start_run(trigger="manual", code_commit="c", catalog_version="v", catalog_commit="k",
                              shard_index=None, code_dirty=True)
        assert store.run(run)["code_dirty"] == 1

    def test_bump_accumulates(self, store):
        run = start(store)
        store.bump_run(run, pages_fetched=2, icons_changed=1)
        store.bump_run(run, pages_fetched=1, icons_failed=0, new_release_skipped=1)
        row = store.run(run)
        assert (row["pages_fetched"], row["icons_changed"], row["icons_failed"]) == (3, 1, 0)
        assert row["new_release_skipped"] == 1

    def test_bump_rejects_unknown_counter_and_run(self, store):
        run = start(store)
        with pytest.raises(ValueError):
            store.bump_run(run, bogus=1)
        with pytest.raises(ValueError):
            store.bump_run(99, pages_fetched=1)

    def test_finish_and_reopen(self, store):
        run = start(store)
        store.finish_run(run, "complete")
        assert store.run(run)["ended_at"] is not None and store.run(run)["status"] == "complete"
        store.reopen_run(run)
        assert store.run(run)["status"] == "running" and store.run(run)["ended_at"] is None
        store.finish_run(run, "abandoned")
        assert store.run(run)["ended_at"] is None
        store.finish_run(run, "failed", "boom")
        assert store.run(run)["error"] == "boom"

    def test_bad_values(self, store):
        run = start(store)
        with pytest.raises(ValueError):
            start(store, trigger="cron")
        with pytest.raises(ValueError):
            store.finish_run(run, "running")
        with pytest.raises(ValueError):
            store.finish_run(run, "done")
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("UPDATE crawl_run SET status = 'done' WHERE run_id = ?", (run,))
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("UPDATE crawl_run SET trigger = 'cron' WHERE run_id = ?", (run,))


class TestRunIdStamping:
    def test_first_200(self, store):
        run = start(store)
        store.record_response(DETAIL, "detail", 200, b"a", item_id="1015655", run_id=run)
        page = store.page(DETAIL)
        assert (page["run_id"], page["changed_run_id"]) == (1, 1)

    def test_identical_and_changed_in_a_later_run(self, store):
        start(store), start(store)
        store.record_response(DETAIL, "detail", 200, b"a", item_id="1015655", run_id=1)
        store.record_response(DETAIL, "detail", 200, b"a", item_id="1015655", run_id=2)
        page = store.page(DETAIL)
        assert (page["run_id"], page["changed_run_id"]) == (2, 1)
        store.record_response(DETAIL, "detail", 200, b"b", item_id="1015655", run_id=2)
        page = store.page(DETAIL)
        assert (page["run_id"], page["changed_run_id"]) == (2, 2)

    def test_304_and_status_only_set_run_id_only(self, store):
        start(store), start(store), start(store)
        store.record_response(DETAIL, "detail", 200, b"a", item_id="1015655", run_id=1)
        store.record_response(DETAIL, "detail", 304, item_id="1015655", run_id=2)
        page = store.page(DETAIL)
        assert (page["run_id"], page["changed_run_id"]) == (2, 1)
        store.record_response(DETAIL, "detail", 503, item_id="1015655", run_id=3)
        page = store.page(DETAIL)
        assert (page["run_id"], page["changed_run_id"], page["status"]) == (3, 1, 503)
        assert store.html(DETAIL) == "a"

    def test_run_id_is_a_foreign_key(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store.record_response(DETAIL, "detail", 200, b"a", item_id="1015655", run_id=42)

    def test_no_run_is_legacy(self, store):
        put_detail(store)
        assert store.page(DETAIL)["run_id"] is None


@dataclass(frozen=True)
class Target:
    url: str
    item_id: str = "1"
    kind: str = "equipment"


PNG = "/images/equipicon/1.png"
BIG = "/images/equipicon/2.png"


class TestAssets:
    def test_put_targets_adds_and_prunes_keeping_bytes(self, store):
        store.put_asset_targets("1", [Target(PNG), Target(BIG)])
        assert [r["url"] for r in store.assets_for("1")] == [PNG, BIG]
        store.record_asset(PNG, 200, b"png", content_type="image/png")
        store.put_asset_targets("1", [Target(PNG)])
        assert [r["url"] for r in store.assets_for("1")] == [PNG]
        assert store.asset_body(PNG) == b"png"

    def test_put_targets_empty_set_drops_the_items_assets_only(self, store):
        store.put_asset_targets("1", [Target(PNG)])
        store.put_asset_targets("2", [Target("/images/equipicon/2.png", "2")])
        store.put_asset_targets("1", [])
        assert store.assets_for("1") == [] and len(store.assets_for("2")) == 1

    def test_unknown_kind(self, store):
        with pytest.raises(ValueError):
            store.put_asset_targets("1", [Target(PNG, kind="monster")])

    def test_put_targets_joins_an_outer_transaction(self, store):
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.put_asset_targets("1", [Target(PNG)])
                raise RuntimeError
        assert store.assets_for("1") == []

    def test_record_200_stores_raw_bytes(self, store):
        run = start(store)
        store.put_asset_targets("1", [Target(PNG)])
        assert store.asset_validators(PNG) is None
        assert store.record_asset(PNG, 200, b"\x89PNGdata", '"e"', "lm", "image/png", run_id=run) is True
        row = store.asset(PNG)
        assert "body" not in row.keys()
        assert store.asset_body(PNG) == b"\x89PNGdata"
        assert row["sha256"] == hashlib.sha256(b"\x89PNGdata").hexdigest()
        assert row["changed_at"] == row["fetched_at"] and row["changed_run_id"] == run
        assert (row["status"], row["content_type"], row["run_id"]) == (200, "image/png", run)
        assert store.asset_validators(PNG) == Validators('"e"', "lm")

    def test_identical_then_different_200(self, store):
        start(store), start(store), start(store)
        store.put_asset_targets("1", [Target(PNG)])
        store.record_asset(PNG, 200, b"a", run_id=1)
        first = store.asset(PNG)
        assert store.record_asset(PNG, 200, b"a", run_id=2) is False
        second = store.asset(PNG)
        assert second["changed_at"] == first["changed_at"] and second["fetched_at"] != first["fetched_at"]
        assert (second["run_id"], second["changed_run_id"]) == (2, 1)
        assert store.record_asset(PNG, 200, b"b", run_id=3) is True
        third = store.asset(PNG)
        assert third["sha256"] == hashlib.sha256(b"b").hexdigest() and third["changed_run_id"] == 3

    def test_304_keeps_body_and_refreshes_validators(self, store):
        store.put_asset_targets("1", [Target(PNG)])
        store.record_asset(PNG, 200, b"a", etag='"1"', last_modified="lm")
        assert store.record_asset(PNG, 304) is False
        row = store.asset(PNG)
        assert row["status"] == 304 and (row["etag"], row["last_modified"]) == ('"1"', "lm")
        store.record_asset(PNG, 304, etag='"2"')
        assert store.asset(PNG)["etag"] == '"2"' and store.asset_body(PNG) == b"a"

    def test_304_without_body_and_wrong_status_are_errors(self, store):
        store.put_asset_targets("1", [Target(PNG)])
        with pytest.raises(ValueError):
            store.record_asset(PNG, 304)
        with pytest.raises(ValueError):
            store.record_asset(PNG, 404)
        with pytest.raises(ValueError):
            store.record_asset(PNG, 200)
        with pytest.raises(ValueError):
            store.record_asset("/nope.png", 200, b"x")

    def test_record_status_keeps_the_bytes(self, store):
        store.put_asset_targets("1", [Target(PNG)])
        store.record_asset(PNG, 200, b"a", etag='"1"', content_type="image/png")
        before = store.asset(PNG)
        store.record_asset_status(PNG, 404)
        row = store.asset(PNG)
        assert row["status"] == 404 and row["fetched_at"] != before["fetched_at"]
        assert (row["sha256"], row["content_type"], row["etag"]) == (before["sha256"], "image/png", '"1"')
        assert store.asset_body(PNG) == b"a"
        with pytest.raises(ValueError):
            store.record_asset_status("/nope.png", 404)

    def test_status_only_on_a_new_target(self, store):
        store.put_asset_targets("1", [Target(PNG)])
        store.record_asset_status(PNG, 404)
        row = store.asset(PNG)
        assert row["sha256"] is None and row["status"] == 404
        assert store.asset_validators(PNG) is None

    def test_unfetched_assets_are_those_of_items_decided_in_the_run(self, store):
        run1, run2 = start(store), start(store)
        for item, run in (("1", run1), ("2", run2), ("3", run2)):
            store.conn.execute(
                "INSERT INTO frontier (item_id, source, entry_kind, keep_reason, discovered_at, updated_at, "
                "decided_run_id) VALUES (?, 'list', 'catalog', 'cost_band', 't', 't', ?)", (item, run))
            store.put_asset_targets(item, [Target(f"/i/{item}.png", item)])
        store.record_asset("/i/3.png", 200, b"x")
        assert [r["url"] for r in store.unfetched_assets(run2)] == ["/i/2.png"]
        assert [r["url"] for r in store.unfetched_assets(run1)] == ["/i/1.png"]


@dataclass(frozen=True)
class Release:
    item_id: str
    name: str = ""


class TestUnlisted:
    def test_streaks(self, store):
        for _ in range(3):
            start(store)
        store.put_unlisted(1, [Release("A", "a"), Release("B", "b")])
        assert [(r["item_id"], r["runs"]) for r in store.unlisted()] == [("A", 1), ("B", 1)]
        store.put_unlisted(2, [Release("A", "a2")])
        rows = {r["item_id"]: r for r in store.unlisted()}
        assert set(rows) == {"A"} and rows["A"]["runs"] == 2 and rows["A"]["name"] == "a2"
        store.put_unlisted(2, [Release("A")])
        assert store.unlisted()[0]["runs"] == 2
        store.put_unlisted(3, [Release("A"), Release("C", "c")])
        rows = {r["item_id"]: r for r in store.unlisted()}
        assert rows["A"]["runs"] == 3 and rows["A"]["first_run_id"] == 1 and rows["A"]["last_run_id"] == 3
        assert rows["C"]["runs"] == 1 and rows["C"]["first_run_id"] == 3

    def test_empty_clears(self, store):
        start(store)
        store.put_unlisted(1, [Release("A")])
        store.put_unlisted(1, [])
        assert store.unlisted() == []


def test_delete_meta(store):
    store.set_meta("k", "v")
    store.delete_meta("k")
    assert store.get_meta("k", "gone") == "gone"
    store.delete_meta("k")
