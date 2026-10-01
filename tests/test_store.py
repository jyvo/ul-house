import itertools
import sqlite3
import zlib
from dataclasses import dataclass

import pytest

from ul_house.seed.store import Store, Validators


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
