import hashlib
import itertools
from collections import namedtuple

import pytest

from fetch_fixtures import UIDS, cached_path, missing
from local_server import LocalServer, RawServer, Reply
from ul_house.crawl.assets import (
    IconTarget,
    fetch_icon,
    fetch_icons,
    icon_targets,
    record_targets,
)
from ul_house.http import robots as robots_mod
from ul_house.http.client import Client
from ul_house.seed.store import Store

ITEM = "1015655"
ICON = f"/images/equipicon/{ITEM}.png"
OTHER = f"/images/equipicon/{ITEM}_b.png"
Target = namedtuple("Target", "url item_id kind")
PNG = b"\x89PNG\r\n\x1a\n"


def ticking_clock():
    counter = itertools.count()
    return lambda: f"2026-10-04T00:00:{next(counter):02d}Z"


def png(tag: str) -> bytes:
    return PNG + tag.encode()


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def conditional(body: bytes, content_type: str = "image/png", etag: str | None = None):
    etag = etag or '"' + sha(body)[:16] + '"'

    def reply(request):
        if request.headers.get("If-None-Match") == etag:
            return Reply(304, headers={"ETag": etag})
        return Reply(200, body, {"ETag": etag, "Content-Type": content_type})
    return reply


@pytest.fixture
def server():
    with LocalServer() as s:
        yield s


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "seed.sqlite", clock=ticking_clock()) as s:
        yield s


@pytest.fixture
def client(server):
    with Client(server.base_url, interval=0.0, max_retries=0, timeout=(5, 5)) as c:
        yield c


def start_run(store) -> int:
    return store.start_run(trigger="manual", code_commit="test", catalog_version="v", catalog_commit="test",
                           shard_index=None)


def target(store, url=ICON, item_id=ITEM):
    store.put_asset_targets(item_id, [Target(url, item_id, "equipment")])


def counters(store, run_id=1):
    row = store.run(run_id)
    return row["icons_fetched"], row["icons_changed"], row["icons_failed"]


class TestTargets:
    @pytest.mark.parametrize("uid", sorted(UIDS))
    def test_existing_pages_icon_derived_from_uid(self, uid):
        if missing():
            pytest.skip(f"fixture pages not cached: {', '.join(missing())}")
        html = cached_path(uid).read_text()
        assert icon_targets(uid, html) == [("equipment", f"/images/equipicon/{uid}.png")]

    def test_icon_does_not_need_page_naming(self):
        assert icon_targets("77", "<html><body>nothing</body></html>") == [("equipment", "/images/equipicon/77.png")]

    def test_icon_type_inside_defined_vocab(self):
        assert {kind for kind, _ in icon_targets("77", "<html></html>")} <= {"equipment", "item", "ability"}

    def test_record_targets_writes_asset_rows(self, store):
        record_targets(store, ITEM, "<html></html>")
        (row,) = store.assets_for(ITEM)
        assert (row["url"], row["item_id"], row["kind"]) == (ICON, ITEM, "equipment")
        assert row["status"] is None and row["fetched_at"] is None

    def test_record_targets_joins_callers_transaction(self, store):
        with pytest.raises(RuntimeError):
            with store.transaction():
                record_targets(store, ITEM, "<html></html>")
                assert len(store.assets_for(ITEM)) == 1
                raise RuntimeError("page transaction fails")
        assert store.assets_for(ITEM) == []

    def test_record_targets_keeps_fetched_bytes(self, store, server, client):
        record_targets(store, ITEM, "<html></html>")
        server.route(ICON, conditional(png("a")))
        fetch_icon(client, store, ICON)
        record_targets(store, ITEM, "<html>changed</html>")
        assert store.asset_body(ICON) == png("a")


def test_valid_icon_path_template():
    # small not large icon path
    from ul_house import config

    assert config.EQUIP_ICON_PATH.format(uid=ITEM) == ICON


class TestFetchIcon:
    @pytest.fixture(autouse=True)
    def _target(self, store):
        target(store)
        self.run_id = start_run(store)

    def test_first_fetch_stores_bytes_and_hash(self, server, store, client):
        body = png("one")
        server.route(ICON, conditional(body))
        result = fetch_icon(client, store, ICON, run_id=1)
        assert (result.url, result.status, result.changed, result.outcome) == (ICON, 200, True, "changed")
        row = store.asset(ICON)
        assert (row["status"], row["sha256"], row["content_type"]) == (200, sha(body), "image/png")
        assert row["etag"] == '"' + sha(body)[:16] + '"'
        assert row["run_id"] == 1 and row["changed_run_id"] == 1
        assert store.asset_body(ICON) == body
        assert counters(store) == (1, 1, 0)

    def test_second_fetch_is_conditional_and_not_modified(self, server, store, client):
        server.route(ICON, conditional(png("one")))
        fetch_icon(client, store, ICON, run_id=1)
        first = store.asset(ICON)
        result = fetch_icon(client, store, ICON, run_id=1)
        assert result.outcome == "not_modified" and result.status == 304 and result.changed is False
        assert server.hits(ICON)[1].headers["If-None-Match"] == first["etag"]
        again = store.asset(ICON)
        assert again["sha256"] == first["sha256"] and again["status"] == 304
        assert again["changed_at"] == first["changed_at"]
        assert again["fetched_at"] != first["fetched_at"]
        assert store.asset_body(ICON) == png("one")
        assert counters(store) == (2, 1, 0)

    def test_new_bytes_with_new_etag_change(self, server, store, client):
        server.route(ICON, conditional(png("one")))
        fetch_icon(client, store, ICON, run_id=1)
        run2 = start_run(store)
        server.route(ICON, conditional(png("two")))
        result = fetch_icon(client, store, ICON, run_id=run2)
        assert result.outcome == "changed" and result.changed is True
        row = store.asset(ICON)
        assert row["sha256"] == sha(png("two"))
        assert (row["run_id"], row["changed_run_id"]) == (run2, run2)
        assert store.asset_body(ICON) == png("two")
        assert counters(store, run2) == (1, 1, 0)

    def test_same_bytes_under_new_etag_unchanged(self, server, store, client):
        server.route(ICON, conditional(png("one"), etag='"v1"'))
        fetch_icon(client, store, ICON, run_id=1)
        before = store.asset(ICON)
        server.route(ICON, conditional(png("one"), etag='"v2"'))
        run2 = start_run(store)
        result = fetch_icon(client, store, ICON, run_id=run2)
        assert result.outcome == "unchanged" and result.changed is False
        row = store.asset(ICON)
        assert row["etag"] == '"v2"'
        assert (row["changed_at"], row["changed_run_id"]) == (before["changed_at"], 1)
        assert row["run_id"] == run2
        assert counters(store, run2) == (1, 0, 0)

    def test_404_is_missing_keeps_old_bytes(self, server, store, client):
        server.route(ICON, conditional(png("one")))
        fetch_icon(client, store, ICON, run_id=1)
        server.route(ICON, Reply(404, b"not found"))
        result = fetch_icon(client, store, ICON, run_id=1)
        assert result.outcome == "missing" and result.status == 404 and result.changed is False
        row = store.asset(ICON)
        assert row["status"] == 404 and row["sha256"] == sha(png("one"))
        assert store.asset_body(ICON) == png("one")
        assert counters(store) == (2, 1, 1)

    def test_html_200_not_an_icon(self, server, store, client):
        server.route(ICON, conditional(png("one")))
        fetch_icon(client, store, ICON, run_id=1)
        server.route(ICON, Reply(200, b"<html>not found</html>", {"Content-Type": "text/html"}))
        result = fetch_icon(client, store, ICON, run_id=1)
        assert result.outcome == "failed" and result.changed is False
        row = store.asset(ICON)
        assert row["status"] == 200 and row["sha256"] == sha(png("one")) and row["content_type"] == "image/png"
        assert store.asset_body(ICON) == png("one")
        assert counters(store) == (2, 1, 1)

    def test_html_200_on_non_fetched_icon_stores_no_bytes(self, server, store, client):
        server.route(ICON, Reply(200, b"<html>soft 404</html>", {"Content-Type": "text/html"}))
        assert fetch_icon(client, store, ICON, run_id=1).outcome == "failed"
        row = store.asset(ICON)
        assert row["status"] == 200 and row["sha256"] is None and row["fetched_at"] is not None
        assert store.asset_body(ICON) is None
        assert counters(store) == (1, 0, 1)

    def test_content_type_matched_case_insensitively(self, server, store, client):
        server.route(ICON, Reply(200, png("x"), {"Content-Type": "IMAGE/PNG"}))
        assert fetch_icon(client, store, ICON, run_id=1).outcome == "changed"

    def test_empty_image_refused(self, server, store, client):
        server.route(ICON, Reply(200, b"", {"Content-Type": "image/png"}))
        assert fetch_icon(client, store, ICON, run_id=1).outcome == "failed"
        assert store.asset_body(ICON) is None

    def test_server_error_failed_and_recorded(self, server, store, client):
        server.route(ICON, Reply(500))
        result = fetch_icon(client, store, ICON, run_id=1)
        assert (result.status, result.outcome) == (500, "failed")
        assert store.asset(ICON)["status"] == 500
        assert counters(store) == (1, 0, 1)

    def test_304_without_stored_bytes_failed(self, server, store, client):
        server.route(ICON, Reply(304, headers={"ETag": '"x"'}))
        result = fetch_icon(client, store, ICON, run_id=1)
        assert result.outcome == "failed"
        assert store.asset_body(ICON) is None

    def test_transport_failure_records_nothing(self, store):
        truncated = b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 100\r\nConnection: close\r\n\r\nshort"
        with RawServer([truncated]) as raw, Client(raw.base_url, interval=0.0, max_retries=0, timeout=(5, 5)) as client:
            result = fetch_icon(client, store, ICON, run_id=1)
        assert result.status is None and result.outcome == "failed" and result.changed is False
        row = store.asset(ICON)
        assert row["fetched_at"] is None and row["status"] is None
        assert counters(store) == (0, 0, 1)

    def test_robots_disallow_is_no_request(self, server, store, client):
        server.route("/robots.txt", Reply(200, b"User-agent: *\nDisallow: /images/\n", {"Content-Type": "text/plain"}))
        server.route(ICON, conditional(png("one")))
        rules = robots_mod.load(client)
        server.log.clear()
        result = fetch_icon(client, store, ICON, run_id=1, robots=rules)
        assert (result.status, result.outcome, result.changed) == (None, "failed", False)
        assert server.hits(ICON) == []
        assert store.asset(ICON)["fetched_at"] is None

    def test_robots_allowing_path_fetches(self, server, store, client):
        server.route("/robots.txt", Reply(200, b"User-agent: *\nDisallow: /private/\n", {"Content-Type": "text/plain"}))
        server.route(ICON, conditional(png("one")))
        assert fetch_icon(client, store, ICON, run_id=1, robots=robots_mod.load(client)).outcome == "changed"

    def test_asset_write_and_counters_commit_together(self, server, store, client):
        server.route(ICON, conditional(png("one")))
        with pytest.raises(RuntimeError):
            with store.transaction():
                fetch_icon(client, store, ICON, run_id=1)
                raise RuntimeError("enclosing transaction fails")
        assert store.asset(ICON)["fetched_at"] is None
        assert counters(store) == (0, 0, 0)

    def test_stored_bytes_raw_not_compressed(self, server, store, client):
        body = png("a" * 400)
        server.route(ICON, conditional(body))
        fetch_icon(client, store, ICON, run_id=1)
        raw = store.conn.execute("SELECT body FROM asset WHERE url = ?", (ICON,)).fetchone()[0]
        assert bytes(raw) == body


class TestWithoutARun:
    def test_no_run_id_needs_no_crawl_row(self, server, store, client):
        target(store)
        server.route(ICON, conditional(png("one")))
        assert fetch_icon(client, store, ICON).outcome == "changed"
        assert store.runs() == []
        row = store.asset(ICON)
        assert row["run_id"] is None and row["changed_run_id"] is None
        assert store.asset_body(ICON) == png("one")


class TestFetchIcons:
    @pytest.fixture(autouse=True)
    def _targets(self, store, server):
        store.put_asset_targets(ITEM, [Target(ICON, ITEM, "equipment"), Target(OTHER, ITEM, "equipment")])
        server.route(ICON, conditional(png("a")))
        server.route(OTHER, conditional(png("b")))
        start_run(store)

    def test_every_asset_of_item_in_url_order(self, server, store, client):
        results = fetch_icons(client, store, ITEM, run_id=1)
        assert [r.url for r in results] == sorted([ICON, OTHER])
        assert all(r.outcome == "changed" for r in results)
        assert counters(store) == (2, 2, 0)

    def test_only_unfetched_skips_requested_before(self, server, store, client):
        fetch_icon(client, store, ICON, run_id=1)
        results = fetch_icons(client, store, ITEM, run_id=1, only_unfetched=True)
        assert [r.url for r in results] == [OTHER]
        assert len(server.hits(ICON)) == 1 and len(server.hits(OTHER)) == 1

    def test_full_sweep_revalidates_conditionally(self, server, store, client):
        fetch_icons(client, store, ITEM, run_id=1)
        results = fetch_icons(client, store, ITEM, run_id=1)
        assert {r.outcome for r in results} == {"not_modified"}
        assert all("If-None-Match" in hit.headers for path in (ICON, OTHER) for hit in server.hits(path)[1:])

    def test_other_items_left_alone(self, server, store, client):
        stranger = "/images/equipicon/42.png"
        store.put_asset_targets("42", [Target(stranger, "42", "equipment")])
        server.route(stranger, conditional(png("s")))
        fetch_icons(client, store, ITEM, run_id=1)
        assert server.hits(stranger) == []

    def test_item_without_targets_is_empty_sweep(self, store, client):
        assert fetch_icons(client, store, "999", run_id=1) == []
