"""e2e crawl on local dummy wiki"""
import hashlib
import os
import re
import signal
import sqlite3
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from fetch_fixtures import cached_path
from local_server import LocalServer, Reply
from mini_seed import (
    MINI_CLOCK,
    MINI_ITEMS,
    FakeWiki,
    WikiItem,
    build_mini_seed,
    fake_png,
    icon_path,
    mini_scope,
)
from ul_house.config import LIST_SOURCES, NEW_RELEASE_PATH, detail_path
from ul_house.crawl.run import CrawlFailed, Lineage, ResumeRefused, crawl, main, shard_of, summary
from ul_house.http.client import Client
from ul_house.seed.store import Store
from ul_house.settings import CATALOG_PATH, Scope, load_scope

LINEAGE = Lineage("test", "test")
A, B, C, R, X = "1001", "1002", "1003", "1004", "1099"
LIST_PATHS = [path for path, _, _ in LIST_SOURCES]


# helpers
def scope_with(**tables) -> Scope:
    """the shipped scope with some table keys replaced: scope_with(revalidate={"cycle_days": 1})"""
    data = load_scope().to_mapping()
    for table, values in tables.items():
        data[table].update(values)
    return Scope.from_mapping(data)


def item(uid, cost=50, rarity="UR", **kw) -> WikiItem:
    return WikiItem(uid, kw.pop("name", f"Item {uid}"), kw.pop("grp", "weapon"), rarity, cost, **kw)


def sync_releases(wiki: FakeWiki, *unlisted: tuple[str, str]) -> FakeWiki:
    """new release page shows every listed item + ids"""
    wiki.releases = [(i.item_id, i.name) for i in wiki.items.values() if i.listed] + list(unlisted)
    return wiki


def wiki_of(*items: WikiItem, unlisted=()) -> FakeWiki:
    return sync_releases(FakeWiki(items), *unlisted)


def uids_in_shards(cycle_days: int, shards) -> list[str]:
    """fresh digit-string id per requested shard (ordered)"""
    found, taken, n = [], set(), 2000
    for shard in shards:
        while shard_of(str(n), cycle_days) != shard or str(n) in taken:
            n += 1
        found.append(str(n))
        taken.add(str(n))
    return found


def detail_hits(server, uid=None):
    if uid is not None:
        return server.hits(detail_path(uid))
    return [seen for seen in server.log if seen.path.startswith("/en/equip_detail/")]


def row_of(store, table, key_col, key):
    row = store.conn.execute(f"SELECT * FROM {table} WHERE {key_col} = ?", (key,)).fetchone()
    return dict(row) if row else None


def frontier(store, uid):
    return row_of(store, "frontier", "item_id", uid)


def meta(store):
    return {r["key"]: r["value"] for r in store.conn.execute("SELECT key, value FROM meta")}


@pytest.fixture
def server():
    with LocalServer() as s:
        yield s


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "seed.sqlite") as s:
        yield s


@pytest.fixture
def client(server):
    with Client(server.base_url, interval=0.0, max_retries=0, timeout=(5, 5)) as c:
        yield c


@pytest.fixture
def go(client, store):
    def run(scope=None, **kw):
        kw.setdefault("lineage", LINEAGE)
        return crawl(client, store, scope or scope_with(), **kw)
    return run


class TestShardOf:
    @pytest.mark.parametrize("uid, cycle, expected", [
        ("1015655", 28, 7), ("4435013", 28, 8), ("4434015", 28, 24), ("9", 4, 0), ("3", 4, 3),
    ])
    def test_vectors(self, uid, cycle, expected):
        assert shard_of(uid, cycle) == expected

    @pytest.mark.parametrize("uid", ["1", "1015655", "99999"])
    def test_one_shard_is_always_zero(self, uid):
        assert shard_of(uid, 1) == 0

    def test_sha256_not_python_hash(self):
        digest = hashlib.sha256(b"1015655").digest()
        assert shard_of("1015655", 28) == int.from_bytes(digest[:8], "big") % 28

    def test_not_dependant_on_pythonhashseed(self):
        code = "from ul_house.crawl.run import shard_of; print(shard_of('1015655', 28), shard_of('4434015', 28))"
        outputs = {
            subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PYTHONHASHSEED": seed}).stdout.strip()
            for seed in ("1", "12345")
        }
        assert outputs == {"7 24"}


class TestLineage:
    def test_github_sha_prio(self, monkeypatch):
        monkeypatch.setenv("GITHUB_SHA", "abc123def")
        assert Lineage.detect().code_commit == "abc123def"

    def test_detect_never_raises_and_always_names_something(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GITHUB_SHA", raising=False)
        found = Lineage.detect(tmp_path / "no-such-catalog.toml")
        assert found.code_commit and found.catalog_commit
        assert found.catalog_commit == "unknown"


class TestArguments:
    @pytest.mark.parametrize("kw", [{"limit": 0}, {"limit": -1}, {"trigger": "cron"}])
    def test_rejected_before_any_row_or_request(self, server, store, go, kw):
        wiki_of(item(A)).install(server)
        with pytest.raises(ValueError):
            go(**kw)
        assert server.log == []
        assert store.runs() == []


class TestFirstRun:
    @pytest.fixture
    def wiki(self, server):
        w = wiki_of(
            item(A, links=(("before", "reforging", R, "Old Sword"),)),
            item(B, cost=60),
            item(C, rarity="SSR"),
            item(R, cost=30, rarity="SSR"),
            unlisted=[(X, "Newcomer")],
        )
        w.install(server)
        return w

    @pytest.mark.usefixtures("wiki")
    def test_first_run_end_to_end(self, server, store, go):
        scope = scope_with()
        result = go(scope)

        assert (result.run_id, result.status, result.resumed, result.stopped_by) == (1, "complete", False, None)
        assert result.shard_index == 0 and result.selected == 2 and result.scope_changed is False
        assert result.vanished == () and result.unlisted == (X,)
        assert result.seconds >= 0

        (run,) = store.runs()
        assert (run["run_id"], run["status"], run["trigger"]) == (1, "complete", "manual")
        assert (run["code_commit"], run["catalog_commit"], run["catalog_version"]) == ("test", "test", scope.fingerprint())
        assert run["started_at"].endswith("Z") and run["ended_at"].endswith("Z") and run["error"] is None
        counted = {k: run[k] for k in ("pages_discovered", "pages_fetched", "pages_changed", "pages_failed",
                                       "pages_not_modified", "icons_fetched", "icons_changed", "icons_failed",
                                       "shard_items")}
        assert counted == dict(pages_discovered=3, pages_fetched=3, pages_changed=3, pages_failed=0,
                               pages_not_modified=0, icons_fetched=3, icons_changed=3, icons_failed=0, shard_items=0)
        assert run["shard_index"] == 0
        assert {k: result.counts[k] for k in counted} == counted

        for uid, depth, source, reason, parent in [(A, 0, "list", "cost_band", None), (B, 0, "list", "cost_band", None),
                                                   (R, 1, "evolution", "evolution:reforge", A)]:
            fr = frontier(store, uid)
            assert (fr["entry_kind"], fr["depth"], fr["source"], fr["keep_reason"], fr["parent_id"], fr["state"],
                    fr["decided_run_id"]) == ("catalog", depth, source, reason, parent, "done", 1), uid
        assert frontier(store, C) is None and frontier(store, X) is None

        for uid in (A, B, R):
            assert store.page(detail_path(uid))["run_id"] == 1
            asset = store.asset(icon_path(uid))
            assert (asset["sha256"], asset["status"], asset["kind"]) == (hashlib.sha256(fake_png(uid)).hexdigest(), 200, "equipment")
            assert store.asset_body(icon_path(uid)) == fake_png(uid)
        assert {r["url"] for r in store.conn.execute("SELECT url FROM page")} == (
            set(LIST_PATHS) | {NEW_RELEASE_PATH} | {detail_path(u) for u in (A, B, R)})
        assert all(r["run_id"] == 1 for r in store.conn.execute("SELECT run_id FROM page"))

        (unlisted,) = store.unlisted()
        assert (unlisted["item_id"], unlisted["name"], unlisted["runs"]) == (X, "Newcomer", 1)
        assert len(detail_hits(server, X)) == 0         # nothing crawled from new-release page

        assert [(l["kind"], l["side"], l["target_id"]) for l in store.links_from([A])] == [("reforge", "before", R)]

        assert meta(store) == {
            "seed.schema_version": "1",
            "scope.stamp": scope.stamp(),
            "scope.fingerprint": scope.fingerprint(),
            "crawl.last_complete_run": "1",
            "revalidate.next_shard": "1",
        }

    @pytest.mark.usefixtures("wiki")
    def test_summary_names_run_and_counts(self, go):
        text = summary(go())
        for token in ("run 1 (complete)", "fetched 3", "changed 3", "failed 0"):
            assert token in text

    @pytest.mark.usefixtures("wiki")
    def test_robots_read_every_invocation(self, server, go):
        go()
        go()
        assert len(server.hits("/robots.txt")) == 2


class TestSecondRun:
    def test_second_run_is_conditional(self, server, store, go):
        a, b, c = uids_in_shards(28, [1, 5, 9])
        wiki_of(item(a), item(b), item(c)).install(server)
        go()
        before = {path: len(server.hits(path)) for path in LIST_PATHS + [NEW_RELEASE_PATH, detail_path(a)]}
        result = go()

        assert result.run_id == 2 and result.status == "complete" and result.shard_index == 1
        for path in LIST_PATHS + [NEW_RELEASE_PATH]:
            second = server.hits(path)[before[path]]
            assert second.headers.get("If-None-Match"), path
        assert [h.path for h in detail_hits(server)[3:]] == [detail_path(a)]        # only shard 1
        assert detail_hits(server, a)[1].headers.get("If-None-Match")
        assert len(detail_hits(server, b)) == 1 and len(detail_hits(server, c)) == 1

        r1, r2 = store.runs()
        assert r2["pages_discovered"] == 0 and r2["pages_fetched"] == 1 and r2["pages_changed"] == 0
        assert (r2["pages_not_modified"], r2["shard_items"], r2["shard_index"]) == (1, 1, 1)
        assert (r2["icons_fetched"], r2["icons_changed"]) == (1, 0)
        assert meta(store)["revalidate.next_shard"] == "2"
        assert store.page(detail_path(a))["run_id"] == 2
        assert store.page(detail_path(b))["run_id"] == 1

    def test_new_item_fetched_immediately_with_icon(self, server, store, go):
        a, b, d = uids_in_shards(28, [5, 6, 7])         # none in run 2's shard
        wiki = wiki_of(item(a), item(b))
        wiki.install(server)
        go()
        wiki.items[d] = item(d)
        sync_releases(wiki).install(server)
        result = go()
        assert result.counts["pages_discovered"] == 1
        assert len(detail_hits(server, d)) == 1 and len(server.hits(icon_path(d))) == 1
        assert store.page(detail_path(d))["changed_run_id"] == 2
        assert store.asset(icon_path(d))["run_id"] == 2 and store.asset(icon_path(d))["changed_run_id"] == 2
        assert store.runs()[1]["pages_changed"] == 1 and store.runs()[1]["icons_changed"] == 1


class TestShard:
    def test_cursor_covers_everything_once_per_cycle(self, server, store, go):
        ids = [str(n) for n in range(1, 11)]
        wiki_of(*(item(u) for u in ids)).install(server)
        scope = scope_with(revalidate={"cycle_days": 4})

        cursors, shard_items = [], []
        for _ in range(5):
            go(scope)
            cursors.append(meta(store)["revalidate.next_shard"])
            shard_items.append(store.runs()[-1]["shard_items"])

        runs = store.runs()
        assert [r["shard_index"] for r in runs] == [0, 1, 2, 3, 0]
        assert cursors == ["1", "2", "3", "0", "1"]
        assert shard_items == [0, 1, 3, 4, 2]
        assert [r["status"] for r in runs] == ["complete"] * 5
        for uid in ids:
            assert len(detail_hits(server, uid)) == 2, uid              # run 1 + own shard's day
        revalidations = detail_hits(server)[len(ids):]
        assert len(revalidations) == 10
        assert all(seen.headers.get("If-None-Match") for seen in revalidations)
        assert {u for u in ids if shard_of(u, 4) == 1} == {"1"}         # documented partition

    def test_page_and_icon_change_seen_by_shard(self, server, store, go):
        wiki = wiki_of(item(A), item(B))
        wiki.install(server)
        scope = scope_with(revalidate={"cycle_days": 1})
        go(scope)
        old_sha = store.asset(icon_path(B))["sha256"]

        wiki.items[B].name = "Item B renamed"
        wiki.items[B].icon = fake_png(B, 2)
        sync_releases(wiki).install(server)
        go(scope)

        page = store.page(detail_path(B))
        assert (page["run_id"], page["changed_run_id"]) == (2, 2)
        asset = store.asset(icon_path(B))
        assert asset["sha256"] == hashlib.sha256(fake_png(B, 2)).hexdigest() != old_sha
        assert (asset["run_id"], asset["changed_run_id"]) == (2, 2)
        assert store.asset(icon_path(A))["changed_run_id"] == 1
        r2 = store.runs()[1]
        assert (r2["pages_changed"], r2["icons_changed"], r2["pages_not_modified"], r2["shard_items"]) == (1, 1, 1, 2)

    def test_404_marks_gone_and_200_revives(self, server, store, go):
        wiki = wiki_of(item(A), item(B))
        wiki.install(server)
        scope = scope_with(revalidate={"cycle_days": 1})
        go(scope)

        wiki.items[B].detail = False
        wiki.install(server)
        go(scope)
        fr = frontier(store, B)
        assert (fr["state"], fr["last_error"]) == ("gone", "404")
        assert store.page(detail_path(B))["status"] == 404
        assert "Item 1002" in store.html(detail_path(B))            # last good bytes survive 404

        wiki.items[B].detail = True
        wiki.install(server)
        go(scope)
        fr = frontier(store, B)
        assert (fr["state"], fr["last_error"]) == ("done", None)
        assert store.page(detail_path(B))["status"] in (200, 304)

    def test_lost_id_stays_in_shard_until_answers(self, server, store, go):
        wiki = wiki_of(item(A), item(B, detail=False))
        wiki.install(server)
        scope = scope_with(revalidate={"cycle_days": 1})
        go(scope)
        go(scope)
        assert frontier(store, B)["state"] == "gone"
        assert len(detail_hits(server, B)) == 2

    def test_scope_change_redecides_shard_only_revalidates_this_runs_decisions(self, server, store, go):
        wiki_of(item(A), item(B, cost=60)).install(server)
        go(scope_with(revalidate={"cycle_days": 1}))
        result = go(scope_with(revalidate={"cycle_days": 1}, cost={"max": 55}))
        assert result.scope_changed is True
        assert (frontier(store, A)["decided_run_id"], frontier(store, B)["decided_run_id"]) == (2, 1)
        assert len(detail_hits(server, A)) == 2
        assert len(detail_hits(server, B)) == 1
        assert meta(store)["scope.fingerprint"] == scope_with(revalidate={"cycle_days": 1}, cost={"max": 55}).fingerprint()


class TestLimitAndResume:
    IDS = [str(n) for n in range(5001, 5007)]

    def test_limit_then_resume(self, server, store, go):
        wiki_of(*(item(u) for u in self.IDS)).install(server)
        first = go(limit=2)
        assert (first.status, first.stopped_by, first.run_id) == ("partial", "limit", 1)
        assert len(detail_hits(server)) == 2
        assert meta(store)["crawl.active_run"] == "1"
        assert "crawl.last_complete_run" not in meta(store)
        assert store.run(1)["status"] == "partial"

        second = go(resume=True)
        assert (second.run_id, second.status, second.resumed, second.stopped_by) == (1, "complete", True, None)
        for uid in self.IDS:
            assert len(detail_hits(server, uid)) == 1, uid
        assert len(store.runs()) == 1
        run = store.run(1)
        assert (run["pages_fetched"], run["pages_changed"], run["icons_fetched"], run["pages_discovered"]) == (6, 6, 6, 6)
        assert "crawl.active_run" not in meta(store) and meta(store)["crawl.last_complete_run"] == "1"

    @pytest.mark.parametrize("limit", [100, 6])
    def test_limit_not_exceeded_is_complete(self, server, go, limit):
        wiki_of(*(item(u) for u in self.IDS)).install(server)
        result = go(limit=limit)
        assert (result.status, result.stopped_by) == ("complete", None)

    def test_limit_counts_each_invocation_afresh(self, server, go):
        wiki_of(*(item(u) for u in self.IDS)).install(server)
        go(limit=2)
        go(limit=2, resume=True)
        assert len(detail_hits(server)) == 4
        assert go(limit=2, resume=True).status == "complete"
        assert len(detail_hits(server)) == 6

    def test_stopping_mid_run_resume_fetches_missing_icon(self, server, store, client, go):
        wiki_of(item(A), item(B), item(C)).install(server)
        inner = server.routes[detail_path(A)]

        def stopping(request):
            client.stop.set()
            return inner(request)
        server.route(detail_path(A), stopping)

        first = go()
        assert (first.status, first.stopped_by) == ("partial", "stop")
        assert store.page(detail_path(A))["status"] == 200              # page committed
        assert store.asset(icon_path(A))["fetched_at"] is None          # icon did not
        assert len(detail_hits(server, B)) == 0
        assert store.run(1)["status"] == "partial" and meta(store)["crawl.active_run"] == "1"

        client.stop.clear()
        server.route(detail_path(A), inner)
        second = go(resume=True)
        assert (second.status, second.run_id) == ("complete", 1)
        assert store.asset_body(icon_path(A)) == fake_png(A)
        assert len(detail_hits(server, A)) == 1
        for uid in (A, B, C):
            assert store.asset_body(icon_path(uid)) == fake_png(uid)

    def test_interrupted_claim_reset_and_attempt_given_back(self, server, store, go):
        wiki_of(*(item(u) for u in self.IDS)).install(server)
        go(limit=1)
        (pending,) = store.conn.execute(
            "SELECT item_id FROM frontier WHERE state = 'pending' ORDER BY item_id LIMIT 1").fetchall()
        store.conn.execute("UPDATE frontier SET state = 'in_flight', attempts = 1 WHERE item_id = ?",
                           (pending["item_id"],))
        result = go(resume=True)
        fr = frontier(store, pending["item_id"])
        assert result.status == "complete"
        assert (fr["state"], fr["attempts"]) == ("done", 1)           # given back then reclaimed
        assert len(detail_hits(server, pending["item_id"])) == 1

    def test_resume_with_nothing_active_starts_fresh(self, server, go):
        wiki_of(item(A)).install(server)
        result = go(resume=True)
        assert (result.run_id, result.resumed, result.status) == (1, False, "complete")


class TestFingerprint:
    def test_mismatch_refuses_resume_and_writes_nothing(self, server, store, go):
        wiki_of(*(item(u) for u in TestLimitAndResume.IDS)).install(server)
        go(limit=1)
        requests_before = len(server.log)
        other = scope_with(cost={"min": 43})
        old_meta = meta(store)

        with pytest.raises(ResumeRefused):
            go(other, resume=True)

        assert len(server.log) == requests_before           # not even robots.txt
        assert len(store.runs()) == 1 and store.run(1)["status"] == "partial"
        assert meta(store) == old_meta

        result = go(other)
        assert (result.run_id, result.status, result.scope_changed, result.resumed) == (2, "complete", True, False)
        assert store.run(1)["status"] == "partial"
        assert meta(store)["scope.fingerprint"] == other.fingerprint()
        assert meta(store)["scope.stamp"] == other.stamp()
        assert store.run(2)["catalog_version"] == other.fingerprint()

    def test_resume_under_same_fingerprint_allowed(self, server, go):
        wiki_of(*(item(u) for u in TestLimitAndResume.IDS)).install(server)
        go(limit=1)
        assert go(resume=True).resumed is True

    def test_fresh_run_abandons_crashed_one(self, server, store, go):
        wiki_of(*(item(u) for u in TestLimitAndResume.IDS)).install(server)
        go(limit=1)
        store.conn.execute("UPDATE crawl_run SET status = 'running', ended_at = NULL WHERE run_id = 1")
        result = go()
        assert result.run_id == 2 and result.status == "complete"
        crashed = store.run(1)
        assert (crashed["status"], crashed["ended_at"]) == ("abandoned", None)

    def test_partial_run_partial_when_fresh_run_starts(self, server, store, go):
        wiki_of(*(item(u) for u in TestLimitAndResume.IDS)).install(server)
        go(limit=1)
        go()
        assert store.run(1)["status"] == "partial"


class TestWalk:
    def test_enlightening_walks_two_deep_and_reforging_one(self, server, store, go):
        e1, e2, r1, r2 = "6001", "6002", "6003", "6004"
        wiki = wiki_of(
            item(A, links=(("before", "enlightening", e1, "E1"), ("before", "reforging", r1, "R1"))),
            item(e1, cost=30, rarity="SSR", listed=False, links=(("before", "enlightening", e2, "E2"),)),
            item(e2, cost=30, rarity="SSR", listed=False),
            item(r1, cost=30, rarity="SSR", listed=False, links=(("before", "reforging", r2, "R2"),)),
            item(r2, cost=30, rarity="SSR", listed=False),
        )
        wiki.install(server)
        go()
        for uid in (A, e1, e2, r1):
            assert frontier(store, uid)["state"] == "done", uid
        assert (frontier(store, e1)["depth"], frontier(store, e2)["depth"], frontier(store, r1)["depth"]) == (1, 2, 1)
        assert frontier(store, e2)["parent_id"] == e1
        assert detail_hits(server, r2) == [] and frontier(store, r2) is None

    def test_name_exclusion_applies_to_reached_target(self, server, store, go):
        n = "6101"
        wiki_of(item(A, links=(("before", "awakening", n, "Awakening Ninoyu X"),)),
                item(n, cost=30, rarity="SSR", listed=False, name="Awakening Ninoyu X")).install(server)
        go()
        assert frontier(store, n) is None and detail_hits(server, n) == []

    def test_reforge_target_outside_rarity_rule_is_catalog(self, server, store, go):
        # under shipping policy/scope
        wiki_of(item(A, links=(("before", "reforging", R, "Old"),)), item(R, cost=30, rarity="SSR")).install(server)
        go()
        assert frontier(store, R)["entry_kind"] == "catalog"


class TestContractAndTransientFailures:
    def test_detail_contract_failure_recorded_and_retried(self, server, store, go):
        wiki_of(item(A, links=(("before", "fusion", "6201", "Something"),)), item(B)).install(server)
        result = go()
        assert result.status == "complete"
        assert store.page(detail_path(A)) is None           # bad bytes never reach the seed
        fr = frontier(store, A)
        assert fr["state"] == "failed" and fr["last_error"].startswith("contract:")
        assert frontier(store, B)["state"] == "done"
        assert store.run(1)["pages_failed"] == 1
        assert store.asset(icon_path(A)) is None or store.asset(icon_path(A))["fetched_at"] is None

        go()
        assert len(detail_hits(server, A)) == 2             # retry_failed at start of every run
        assert frontier(store, A)["last_error"].startswith("contract:")

    def test_contract_failure_on_known_page_keeps_old_bytes(self, server, store, go):
        wiki = wiki_of(item(A))
        wiki.install(server)
        scope = scope_with(revalidate={"cycle_days": 1})
        go(scope)
        good = store.html(detail_path(A))
        wiki.items[A].links = (("before", "fusion", "6201", "Something"),)
        wiki.install(server)
        go(scope)
        assert store.html(detail_path(A)) == good
        fr = frontier(store, A)
        assert fr["state"] == "done" and fr["last_error"].startswith("contract:")   # shard keeps the state

    def test_transient_failure_retried_next_run(self, server, store, go):
        wiki = wiki_of(item(A), item(B))
        wiki.install(server)
        server.route(detail_path(B), [Reply(503)])
        result = go()
        assert result.status == "complete"
        assert len(detail_hits(server, B)) == 3         # MAX_ATTEMPTS claims, one request each
        fr = frontier(store, B)
        assert fr["state"] == "failed" and fr["last_error"].startswith("http:")
        assert store.run(1)["pages_failed"] >= 1

        wiki.install(server)
        go()
        assert frontier(store, B)["state"] == "done" and frontier(store, B)["last_error"] is None
        assert store.asset_body(icon_path(B)) == fake_png(B)


class TestRobotsAndDiscovery:
    def test_robots_unreachable_fails_closed(self, server, store, go):
        wiki = wiki_of(item(A))
        wiki.robots = Reply(503)
        wiki.install(server)
        with pytest.raises(CrawlFailed) as caught:
            go()
        assert caught.value.run_id == 1
        run = store.run(1)
        assert run["status"] == "failed" and "robots" in run["error"]
        assert {seen.path for seen in server.log} == {"/robots.txt"}
        assert "crawl.active_run" not in meta(store)
        assert "crawl.last_complete_run" not in meta(store)

    def test_robots_disallowing_detail_page(self, server, store, go):
        wiki = wiki_of(item("2001"), item("2002"))
        wiki.robots = Reply(200, b"User-agent: *\nDisallow: /en/equip_detail/2002.html\n", {"Content-Type": "text/plain"})
        wiki.install(server)
        result = go()
        assert result.status == "complete"
        assert detail_hits(server, "2002") == []
        fr = frontier(store, "2002")
        assert fr["state"] == "failed" and fr["last_error"].startswith("robots:")
        assert frontier(store, "2001")["state"] == "done"

    def test_robots_disallowing_list_page_fails_run(self, server, store, go):
        wiki = wiki_of(item(A))
        wiki.robots = Reply(200, f"User-agent: *\nDisallow: {LIST_PATHS[1]}\n".encode(), {"Content-Type": "text/plain"})
        wiki.install(server)
        with pytest.raises(CrawlFailed):
            go()
        assert store.run(1)["status"] == "failed" and "robots" in store.run(1)["error"]
        assert detail_hits(server) == []

    def test_list_page_failure_fails_closed(self, server, store, go):
        wiki_of(item(A), item(B)).install(server)
        server.route(LIST_PATHS[0], Reply(503))
        with pytest.raises(CrawlFailed):
            go()
        assert detail_hits(server) == []
        assert store.listing_all() == []
        run = store.run(1)
        assert run["status"] == "failed" and run["error"]
        assert "crawl.active_run" not in meta(store)

    def test_malformed_list_page_fails_closed(self, server, go):
        wiki_of(item(A)).install(server)
        server.route(LIST_PATHS[0], Reply(200, b"<html>maintenance</html>", {"Content-Type": "text/html"}))
        with pytest.raises(CrawlFailed):
            go()
        assert detail_hits(server) == []

    @pytest.mark.parametrize("reply", [Reply(503), Reply(200, b"<html>maintenance</html>", {"Content-Type": "text/html"})],
                             ids=["http-503", "no-links"])
    def test_broken_new_release_page_does_not_fail_run(self, server, store, go, reply):
        wiki_of(item(A)).install(server)
        server.route(NEW_RELEASE_PATH, reply)
        result = go()
        assert result.status == "complete" and result.unlisted == ()
        run = store.run(1)
        assert run["new_release_skipped"] == 1 and run["error"] is None
        assert frontier(store, A)["state"] == "done"
        assert store.unlisted() == []

    def test_robots_disallowed_new_release_page_skips_crosscheck(self, server, store, go):
        wiki = wiki_of(item(A))
        wiki.robots = Reply(200, f"User-agent: *\nDisallow: {NEW_RELEASE_PATH}\n".encode(), {"Content-Type": "text/plain"})
        wiki.install(server)
        result = go()
        assert result.status == "complete" and result.unlisted == ()
        run = store.run(1)
        assert run["new_release_skipped"] == 1 and run["error"] is None
        assert server.hits(NEW_RELEASE_PATH) == []
        assert frontier(store, A)["state"] == "done"

    def test_healthy_new_release_page_not_skipped(self, server, store, go):
        wiki_of(item(A)).install(server)
        go()
        assert store.run(1)["new_release_skipped"] == 0


class TestUnlistedStreak:
    def test_streak_grows_then_breaks(self, server, store, go):
        wiki = wiki_of(item(A), unlisted=[(X, "Newcomer")])
        wiki.install(server)
        go()
        go()
        (row,) = store.unlisted()
        assert (row["item_id"], row["runs"], row["first_run_id"], row["last_run_id"]) == (X, 2, 1, 2)
        assert detail_hits(server, X) == []

        wiki.items[X] = item(X, name="Newcomer")
        sync_releases(wiki).install(server)
        go()
        assert store.unlisted() == []

    def test_resumed_run_not_counted_twice(self, server, store, go):
        wiki_of(*(item(u) for u in TestLimitAndResume.IDS), unlisted=[(X, "Newcomer")]).install(server)
        go(limit=1)
        assert store.unlisted()[0]["runs"] == 1
        go(resume=True)
        assert store.unlisted()[0]["runs"] == 1


@pytest.fixture
def catalog(tmp_path):
    """temp copy of shipped catalog with fast request interval"""
    text = CATALOG_PATH.read_text()
    text, hits = re.subn(r"(request_interval\s*=\s*)[0-9.]+", r"\g<1>0.001", text)
    assert hits == 1
    path = tmp_path / "catalog.toml"
    path.write_text(text)
    return path


def other_catalog(catalog, tmp_path):
    text, hits = re.subn(r"(\bmin\s*=\s*)42", r"\g<1>43", catalog.read_text(), count=1)
    assert hits == 1
    path = tmp_path / "other.toml"
    path.write_text(text)
    return path


def runs_in(seed):
    conn = sqlite3.connect(seed)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM crawl_run ORDER BY run_id")]
    finally:
        conn.close()


class TestCli:
    @pytest.fixture
    def site(self, server):
        wiki_of(*(item(u) for u in TestLimitAndResume.IDS)).install(server)
        return server

    def args(self, tmp_path, site, catalog, *extra, seed="seed.sqlite"):
        return ["--seed", str(tmp_path / seed), "--base-url", site.base_url, "--catalog", str(catalog), *extra]

    def test_init_creates_seed_and_runs(self, tmp_path, site, catalog, capsys):
        assert main(self.args(tmp_path, site, catalog, "--init")) == 0
        assert "run 1 (complete)" in capsys.readouterr().out
        (run,) = runs_in(tmp_path / "seed.sqlite")
        assert (run["status"], run["trigger"]) == ("complete", "manual")

    def test_missing_seed_without_init_refused(self, tmp_path, site, catalog):
        assert main(self.args(tmp_path, site, catalog)) == 2
        assert not (tmp_path / "seed.sqlite").exists()
        assert site.log == []

    def test_existing_seed_needs_no_init(self, tmp_path, site, catalog):
        assert main(self.args(tmp_path, site, catalog, "--init")) == 0
        assert main(self.args(tmp_path, site, catalog)) == 0
        assert [r["run_id"] for r in runs_in(tmp_path / "seed.sqlite")] == [1, 2]

    def test_limit_partial_but_exits_and_resume_refuses_another_fingerprint(self, tmp_path, site, catalog):
        assert main(self.args(tmp_path, site, catalog, "--init", "--limit", "1")) == 0
        assert [r["status"] for r in runs_in(tmp_path / "seed.sqlite")] == ["partial"]
        assert main(self.args(tmp_path, site, other_catalog(catalog, tmp_path), "--resume")) == 2
        assert [r["status"] for r in runs_in(tmp_path / "seed.sqlite")] == ["partial"]
        assert main(self.args(tmp_path, site, catalog, "--resume")) == 0
        assert [r["status"] for r in runs_in(tmp_path / "seed.sqlite")] == ["complete"]

    def test_limit_zero_is_usage_error(self, tmp_path, site, catalog):
        with pytest.raises(SystemExit) as caught:
            main(self.args(tmp_path, site, catalog, "--init", "--limit", "0"))
        assert caught.value.code == 2

    def test_trigger_and_commits_recorded(self, tmp_path, site, catalog):
        assert main(self.args(tmp_path, site, catalog, "--init", "--trigger", "schedule",
                              "--code-commit", "abc", "--catalog-commit", "def")) == 0
        (run,) = runs_in(tmp_path / "seed.sqlite")
        assert (run["trigger"], run["code_commit"], run["catalog_commit"]) == ("schedule", "abc", "def")

    def test_unknown_trigger_is_usage_error(self, tmp_path, site, catalog):
        with pytest.raises(SystemExit) as caught:
            main(self.args(tmp_path, site, catalog, "--init", "--trigger", "cron"))
        assert caught.value.code == 2

    def test_a_bad_catalog_refused(self, tmp_path, site):
        bad = tmp_path / "bad.toml"
        bad.write_text("[cost]\nmin = 1\n")
        assert main(self.args(tmp_path, site, bad, "--init")) == 2
        assert site.log == []

    def test_failed_run_exits_one(self, tmp_path, server, catalog):
        wiki = wiki_of(item(A))
        wiki.robots = Reply(200, f"User-agent: *\nDisallow: {LIST_PATHS[0]}\n".encode(), {"Content-Type": "text/plain"})
        wiki.install(server)
        assert main(self.args(tmp_path, server, catalog, "--init")) == 1
        assert [r["status"] for r in runs_in(tmp_path / "seed.sqlite")] == ["failed"]

    def test_help_exits_zero(self):
        done = subprocess.run([sys.executable, "-m", "ul_house.crawl.run", "--help"], capture_output=True,
                              text=True, timeout=60)
        assert done.returncode == 0
        for flag in ("--seed", "--init", "--resume", "--limit", "--base-url", "--catalog", "--trigger",
                     "--code-commit", "--catalog-commit"):
            assert flag in done.stdout

    def test_sigint_stops_run_and_leaves_resumable(self, tmp_path, site, catalog):
        holder, fired = {}, []
        inner = site.routes[detail_path(TestLimitAndResume.IDS[0])]

        def interrupting(request):
            if not fired:
                fired.append(True)
                os.kill(holder["proc"].pid, signal.SIGINT)
            return inner(request)
        site.route(detail_path(TestLimitAndResume.IDS[0]), interrupting)

        seed = tmp_path / "seed.sqlite"
        proc = subprocess.Popen(
            [sys.executable, "-m", "ul_house.crawl.run", "--seed", str(seed), "--init", "--base-url", site.base_url,
             "--catalog", str(catalog)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        holder["proc"] = proc
        try:
            out, err = proc.communicate(timeout=90)
        finally:
            if proc.poll() is None:
                proc.kill()
        assert proc.returncode == 130, (out, err)
        assert [r["status"] for r in runs_in(seed)] == ["partial"]
        assert main(self.args(tmp_path, site, catalog, "--resume")) == 0
        assert [r["status"] for r in runs_in(seed)] == ["complete"]


MINI_UIDS = [i.item_id for i in MINI_ITEMS]
MINI_GONE = ["1014667", "1500501", "1796603", "1890423", "1015065", "4424110"]


@pytest.fixture(scope="module")
def seed(tmp_path_factory):
    return build_mini_seed(tmp_path_factory.mktemp("mini") / "seed.sqlite")


class TestMiniSeed:
    @pytest.fixture
    def conn(self, seed):
        conn = sqlite3.connect(seed)
        conn.row_factory = sqlite3.Row
        yield conn
        conn.close()

    def q(self, conn, sql, *args):
        return [dict(r) for r in conn.execute(sql, args)]

    def test_file_self_contained_and_never_overwritten(self, seed):
        assert seed.exists()
        assert not Path(f"{seed}-wal").exists() and not Path(f"{seed}-shm").exists()
        with pytest.raises(FileExistsError):
            build_mini_seed(seed)

    def test_crawl_run(self, conn):
        (run,) = self.q(conn, "SELECT * FROM crawl_run")
        assert run["catalog_version"] == mini_scope().fingerprint()
        assert {k: run[k] for k in (
            "run_id", "status", "trigger", "code_commit", "catalog_commit", "pages_discovered", "pages_fetched",
            "pages_changed", "pages_failed", "pages_not_modified", "icons_fetched", "icons_changed", "icons_failed",
            "shard_index", "shard_items", "started_at", "ended_at", "error", "code_dirty", "new_release_skipped",
        )} == dict(run_id=1, status="complete", trigger="manual", code_commit="mini-seed", catalog_commit="mini-seed",
                   pages_discovered=14, pages_fetched=14, pages_changed=8, pages_failed=0, pages_not_modified=0,
                   icons_fetched=8, icons_changed=8, icons_failed=0, shard_index=0, shard_items=0,
                   started_at=MINI_CLOCK, ended_at=MINI_CLOCK, error=None, code_dirty=0, new_release_skipped=0)

    def test_listing(self, conn):
        rows = self.q(conn, "SELECT item_id, name, cost, source_url FROM listing ORDER BY item_id")
        assert [r["item_id"] for r in rows] == sorted(MINI_UIDS + ["99100001", "99300001"])
        by_id = {r["item_id"]: r for r in rows}
        assert by_id["99100001"]["source_url"] == "/en/equip_list/1_4.html"
        assert by_id["99300001"]["source_url"] == "/en/equip_list/23_4.html"
        assert (by_id["99100001"]["name"], by_id["99100001"]["cost"]) == ("filler", 99)
        assert (by_id["99300001"]["name"], by_id["99300001"]["cost"]) == ("filler", 99)
        for mini in MINI_ITEMS:
            assert (by_id[mini.item_id]["name"], by_id[mini.item_id]["cost"]) == (mini.name, mini.cost)

    def test_frontier(self, conn):
        rows = {r["item_id"]: r for r in self.q(conn, "SELECT * FROM frontier")}
        assert len(rows) == 14 and all(r["decided_run_id"] == 1 for r in rows.values())

        depth0 = {"1015655": "cost_band", "1890424": "cost_band", "1796604": "cost_band", "1500502": "cost_band",
                  "4425111": "cost_band", "4435013": "cost_band", "1015157": "name_token"}
        for uid, reason in depth0.items():
            r = rows[uid]
            assert (r["source"], r["entry_kind"], r["keep_reason"], r["depth"], r["parent_id"], r["state"]) == (
                "list", "catalog", reason, 0, None, "done"), uid

        r = rows["4434015"]
        assert (r["source"], r["entry_kind"], r["keep_reason"], r["depth"], r["parent_id"], r["state"]) == (
            "evolution", "catalog", "evolution:reforge", 1, "4435013", "done")

        gone = {"1014667": ("catalog", "evolution:reforge", "1015655"), "1500501": ("catalog", "evolution:reforge", "1500502"),
                "1796603": ("catalog", "evolution:reforge", "1796604"), "1890423": ("catalog", "evolution:reforge", "1890424"),
                "1015065": ("reference", "evolution:awakening", "1015157"),
                "4424110": ("reference", "evolution:awakening", "4425111")}
        for uid, (kind, reason, parent) in gone.items():
            r = rows[uid]
            assert (r["source"], r["entry_kind"], r["keep_reason"], r["depth"], r["parent_id"], r["state"]) == (
                "evolution", kind, reason, 1, parent, "gone"), uid
            assert r["last_error"] == "404"

    def test_page(self, conn):
        pages = {r["url"]: r for r in self.q(conn, "SELECT * FROM page")}
        assert len(pages) == 21 and all(p["run_id"] == 1 for p in pages.values())
        assert sorted(p["kind"] for p in pages.values()).count("list") == 6
        assert pages["/en/new_release_list.html"]["kind"] == "new_release"
        for path in LIST_PATHS + [NEW_RELEASE_PATH]:
            assert pages[path]["status"] == 200 and pages[path]["changed_run_id"] == 1
        for uid in MINI_UIDS:
            p = pages[detail_path(uid)]
            assert (p["kind"], p["item_id"], p["status"], p["changed_run_id"]) == ("detail", uid, 200, 1)
            assert zlib.decompress(p["html"]) == cached_path(uid).read_bytes()
        for uid in MINI_GONE:
            p = pages[detail_path(uid)]
            assert (p["status"], p["html"]) == (404, None)

    def test_evo_link(self, conn):
        from test_links import EXPECTED

        got = self.q(conn, "SELECT source_id, kind, side, target_id FROM evo_link ORDER BY source_id, kind, side")
        want = sorted((s, k, side, t) for s, links in EXPECTED.items() for k, side, t in links)
        assert [(r["source_id"], r["kind"], r["side"], r["target_id"]) for r in got] == want
        assert len(got) == 10

    def test_asset(self, conn):
        rows = {r["url"]: r for r in self.q(conn, "SELECT * FROM asset")}
        assert set(rows) == {icon_path(u) for u in MINI_UIDS}
        for uid in MINI_UIDS:
            r = rows[icon_path(uid)]
            body = fake_png(uid)
            assert (r["item_id"], r["kind"], r["status"], r["content_type"]) == (uid, "equipment", 200, "image/png")
            assert bytes(r["body"]) == body and r["sha256"] == hashlib.sha256(body).hexdigest()

    def test_unlisted_release(self, conn):
        (row,) = self.q(conn, "SELECT * FROM unlisted_release")
        assert (row["item_id"], row["name"], row["runs"], row["first_run_id"], row["last_run_id"]) == (
            "1999999", "Unlisted Newcomer", 1, 1, 1)

    def test_meta(self, conn):
        scope = mini_scope()
        got = {r["key"]: r["value"] for r in self.q(conn, "SELECT key, value FROM meta")}
        assert got == {
            "seed.schema_version": "1",
            "scope.stamp": scope.stamp(),
            "scope.fingerprint": scope.fingerprint(),
            "crawl.last_complete_run": "1",
            "revalidate.next_shard": "1",
        }

    def test_mini_scope_is_shipped_scope_with_cost_min_35(self):
        shipped = load_scope().to_mapping()
        mini = mini_scope().to_mapping()
        assert mini["cost"]["min"] == 35
        mini["cost"]["min"] = shipped["cost"]["min"]
        assert mini == shipped
