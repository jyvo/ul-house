"""one crawl run: robots > discover > select > fetch > links > walk > shard > icons > crawl_run"""
from __future__ import annotations

import argparse
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ul_house.config import BASE_URL, NEW_RELEASE_PATH, detail_path
from ul_house.crawl import assets
from ul_house.crawl.discover import SOURCES, DiscoveryError, ListMarkupError, ListRow, discover
from ul_house.crawl.evolution import expand
from ul_house.crawl.frontier import Frontier
from ul_house.crawl.links import LinkMarkupError, record_links
from ul_house.http import robots as robots_mod
from ul_house.http.client import Client, FetchError, Stopped
from ul_house.select import select
from ul_house.seed.store import RUN_COUNTERS, RUN_TRIGGERS, SeedSchemaError, Store
from ul_house.settings import CATALOG_PATH, Scope, ScopeError, load_scope

MAX_ATTEMPTS = 3
META_ACTIVE_RUN = "crawl.active_run"
META_LAST_COMPLETE = "crawl.last_complete_run"
META_SCOPE_STAMP = "scope.stamp"
META_SCOPE_FINGERPRINT = "scope.fingerprint"
META_NEXT_SHARD = "revalidate.next_shard"

EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_STOPPED = 0, 1, 2, 130
GIT_TIMEOUT = 10


class CrawlError(RuntimeError):
    pass


class ResumeRefused(CrawlError):
    """resume under a scope fingerprint other than the one the active run started under"""


class CrawlFailed(CrawlError):
    """the run was recorded as 'failed' with this reason before raising"""

    def __init__(self, reason: str, run_id: int):
        super().__init__(reason)
        self.run_id = run_id


class _LimitReached(Exception):
    pass


class _Fail(Exception):
    """internal: becomes CrawlFailed once the run row is closed"""


@dataclass(frozen=True)
class Lineage:
    code_commit: str
    catalog_commit: str
    code_dirty: bool = False

    @classmethod
    def detect(cls, catalog_path: Path = CATALOG_PATH) -> Lineage:
        repo = Path(__file__).resolve().parents[3]

        def git(*args: str) -> str | None:
            try:
                done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                                      timeout=GIT_TIMEOUT, check=False)
            except (OSError, subprocess.SubprocessError):
                return None
            return done.stdout.strip() if done.returncode == 0 else None

        code = os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD") or "unknown"
        catalog = git("log", "-1", "--format=%H", "--", str(Path(catalog_path).resolve())) or "unknown"
        dirty = git("status", "--porcelain")
        return cls(code, catalog, bool(dirty))


@dataclass(frozen=True)
class RunResult:
    run_id: int
    status: str                     # complete | partial
    resumed: bool
    stopped_by: str | None          # None | limit | stop
    shard_index: int
    selected: int
    vanished: tuple[str, ...]       # Discovery.vanished
    unlisted: tuple[str, ...]       # Discovery.unlisted_releases ids
    scope_changed: bool             # fresh run under a fingerprint
    counts: dict[str, int]
    seconds: float


def shard_of(item_id: str, cycle_days: int) -> int:
    return int.from_bytes(hashlib.sha256(item_id.encode("utf-8")).digest()[:8], "big") % cycle_days


class _Run:
    def __init__(self, client: Client, store: Store, scope: Scope, frontier: Frontier, run_id: int,
                 shard: int, limit: int | None):
        self.client, self.store, self.scope, self.frontier = client, store, scope, frontier
        self.run_id, self.shard, self.limit = run_id, shard, limit
        self.requests = 0
        self.robots: robots_mod.Robots | None = None
        self.selected = 0
        self.vanished: tuple[str, ...] = ()
        self.unlisted: tuple[str, ...] = ()

    def bump(self, **deltas: int) -> None:
        self.store.bump_run(self.run_id, **deltas)

    def check_limit(self) -> None:
        if self.limit is not None and self.requests >= self.limit:
            raise _LimitReached

    def load_robots(self) -> None:
        robots = robots_mod.load(self.client)
        if robots.state == "unreachable":
            status = f"HTTP {robots.status}" if robots.status is not None else "no response"
            raise _Fail(f"robots: unreachable ({status})")
        robots_mod.apply(robots, self.client)
        for source in SOURCES:
            if not robots.allowed(source.path):
                raise _Fail(f"robots: list page disallowed: {source.path}")
        self.robots = robots

    def discover(self) -> None:
        new_release = self.robots.allowed(NEW_RELEASE_PATH)
        try:
            discovery = discover(self.client, self.store, new_release=new_release, run_id=self.run_id)
        except (DiscoveryError, ListMarkupError, FetchError) as exc:
            raise _Fail(f"discover: {exc}") from exc
        self.vanished = tuple(discovery.vanished)
        self.unlisted = tuple(row.item_id for row in discovery.unlisted_releases)
        if not new_release or discovery.new_release_error is not None:
            if not self.store.run(self.run_id)["new_release_skipped"]:
                self.bump(new_release_skipped=1)
        else:
            self.store.put_unlisted(self.run_id, discovery.unlisted_releases)

    def select(self) -> None:
        rows = [ListRow.from_db(row) for row in self.store.listing_all()]
        entries = select(rows, self.scope).entries()
        self.selected = len(entries)
        counts = self.frontier.enqueue_all(entries, self.run_id)
        self.bump(pages_discovered=counts["new"])

    def fetch_pending(self) -> None:
        while self.frontier.pending_count(self.run_id) > 0:
            self.check_limit()
            entry = self.frontier.claim_next(self.run_id)
            if entry is None:
                return
            self.detail(entry.item_id, claimed=True)

    def walk(self) -> None:
        for depth in range(1, self.scope.evolution.max_depth + 1):
            outcome = expand(self.store, self.frontier, self.scope, depth, self.run_id)
            self.bump(pages_discovered=outcome["new"])
            self.fetch_pending()

    def revalidate_shard(self) -> None:
        cycle = self.scope.revalidate.cycle_days
        for entry in self.frontier.decided(self.run_id, ("done", "gone")):
            if shard_of(entry.item_id, cycle) != self.shard:
                continue
            page = self.store.page(detail_path(entry.item_id))
            if page is not None and page["run_id"] == self.run_id:
                continue                        # already fetched in run
            self.check_limit()
            self.detail(entry.item_id, claimed=False)

    def sweep_icons(self) -> None:
        for row in self.store.unfetched_assets(self.run_id):
            assets.fetch_icon(self.client, self.store, row["url"], run_id=self.run_id, robots=self.robots)

    def _fail(self, item_id: str, claimed: bool, error: str, *, blocked: bool = False) -> None:
        if claimed:
            if blocked:
                self.frontier.mark_blocked(item_id, error)
            else:
                self.frontier.mark_failed(item_id, error)
        else:
            self.frontier.revalidated(item_id, self.frontier.get(item_id).state, error)

    def detail(self, item_id: str, *, claimed: bool) -> None:
        store, frontier = self.store, self.frontier
        path = detail_path(item_id)
        shard = {} if claimed else {"shard_items": 1}

        if not self.robots.allowed(path):
            with store.transaction():
                self._fail(item_id, claimed, "robots: disallowed", blocked=True)
                self.bump(pages_failed=1, **shard)
            return

        validators = store.validators(path)
        self.requests += 1
        try:
            reply = self.client.get(path, validators.headers() if validators else None)
        except FetchError as exc:
            with store.transaction():
                self._fail(item_id, claimed, f"fetch: {exc}")
                self.bump(pages_failed=1, **shard)
            return

        status = reply.status
        if status == 200 or (status == 304 and validators is not None):
            html = reply.body.decode("utf-8", errors="replace") if status == 200 else store.html(path)
            try:
                with store.transaction():
                    changed = store.record_response(
                        path, "detail", status, reply.body, reply.etag, reply.last_modified,
                        reply.content_type, item_id=item_id, run_id=self.run_id)
                    record_links(store, item_id, html)
                    assets.record_targets(store, item_id, html)
                    if claimed:
                        frontier.mark_done(item_id)
                    else:
                        frontier.revalidated(item_id, "done", None)
                    self.bump(pages_fetched=1, pages_changed=int(changed), pages_not_modified=int(status == 304), **shard)
            except LinkMarkupError as exc:
                with store.transaction():
                    self._fail(item_id, claimed, f"contract: {exc}", blocked=True)
                    self.bump(pages_fetched=1, pages_failed=1, **shard)
                return
            assets.fetch_icons(self.client, store, item_id, run_id=self.run_id, robots=self.robots,
                               only_unfetched=claimed)
            return

        if status == 404:
            with store.transaction():
                store.record_response(path, "detail", 404, item_id=item_id, run_id=self.run_id)
                if claimed:
                    frontier.mark_gone(item_id, "404")
                else:
                    frontier.revalidated(item_id, "gone", "404")
                self.bump(pages_fetched=1, **shard)
            return

        if status == 304:
            with store.transaction():
                self._fail(item_id, claimed, "http: 304 without validators")
                self.bump(pages_fetched=1, pages_failed=1, **shard)
            return

        note = f" redirect to {reply.redirect_refused} refused" if reply.redirect_refused else ""
        with store.transaction():
            store.record_response(path, "detail", status, item_id=item_id, run_id=self.run_id)
            self._fail(item_id, claimed, f"http: {status}{note}")
            self.bump(pages_fetched=1, pages_failed=1, **shard)


def crawl(client: Client, store: Store, scope: Scope, *, lineage: Lineage, limit: int | None = None,
          resume: bool = False, trigger: str = "manual", max_attempts: int = MAX_ATTEMPTS) -> RunResult:
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    if trigger not in RUN_TRIGGERS:
        raise ValueError(f"unknown trigger {trigger!r}")

    started = time.monotonic()
    fingerprint = scope.fingerprint()
    frontier = Frontier(store, max_attempts)
    active = store.get_meta(META_ACTIVE_RUN)
    cycle = scope.revalidate.cycle_days

    scope_changed = False
    if resume and active is not None and store.run(int(active)) is not None:
        previous = store.get_meta(META_SCOPE_FINGERPRINT)
        if previous != fingerprint:
            raise ResumeRefused(f"active run {active} started under scope {str(previous)[:8]}, "
                                f"this is {fingerprint[:8]}")
        run_id = int(active)
        store.reopen_run(run_id)
        shard = store.run(run_id)["shard_index"]
        resumed = True
    else:
        resumed = False
        if active is not None:
            old = store.run(int(active))
            if old is not None and old["status"] == "running":
                store.finish_run(int(active), "abandoned")
        shard = int(store.get_meta(META_NEXT_SHARD) or 0) % cycle
        previous = store.get_meta(META_SCOPE_FINGERPRINT)
        scope_changed = previous is not None and previous != fingerprint
        with store.transaction():
            run_id = store.start_run(trigger=trigger, code_commit=lineage.code_commit,
                                     catalog_version=fingerprint, catalog_commit=lineage.catalog_commit,
                                     shard_index=shard, code_dirty=lineage.code_dirty)
            store.set_meta(META_ACTIVE_RUN, str(run_id))
            store.set_meta(META_SCOPE_STAMP, scope.stamp())
            store.set_meta(META_SCOPE_FINGERPRINT, fingerprint)

    run = _Run(client, store, scope, frontier, run_id, shard, limit)
    stopped_by = None
    try:
        with store.transaction():
            frontier.reset_in_flight()
            frontier.retry_failed()
        run.load_robots()
        run.discover()
        run.select()
        run.fetch_pending()
        run.walk()
        run.revalidate_shard()
        run.sweep_icons()
        with store.transaction():
            store.finish_run(run_id, "complete")
            store.set_meta(META_NEXT_SHARD, str((shard + 1) % cycle))
            store.set_meta(META_LAST_COMPLETE, str(run_id))
            store.delete_meta(META_ACTIVE_RUN)
        status = "complete"
    except _LimitReached:
        store.finish_run(run_id, "partial")
        status, stopped_by = "partial", "limit"
    except Stopped:
        store.finish_run(run_id, "partial")
        status, stopped_by = "partial", "stop"
    except _Fail as exc:
        _close_failed(store, run_id, str(exc))
        raise CrawlFailed(str(exc), run_id) from exc.__cause__
    except Exception as exc:
        _close_failed(store, run_id, repr(exc))
        raise

    row = store.run(run_id)
    return RunResult(
        run_id=run_id, status=status, resumed=resumed, stopped_by=stopped_by, shard_index=shard,
        selected=run.selected, vanished=run.vanished, unlisted=run.unlisted, scope_changed=scope_changed,
        counts={name: row[name] for name in RUN_COUNTERS}, seconds=time.monotonic() - started,
    )


def _close_failed(store: Store, run_id: int, reason: str) -> None:
    with store.transaction():
        store.finish_run(run_id, "failed", reason)
        store.delete_meta(META_ACTIVE_RUN)


def summary(result: RunResult) -> str:
    c = result.counts
    return "\n".join([
        f"Crawl      run {result.run_id} ({result.status}) · {result.seconds:.0f} s"
        f"{' · resumed' if result.resumed else ''}"
        f"{f' · stopped by {result.stopped_by}' if result.stopped_by else ''}",
        f"  new ids {c['pages_discovered']} · shard {result.shard_index} ({c['shard_items']}) · "
        f"fetched {c['pages_fetched']} · changed {c['pages_changed']} · 304 {c['pages_not_modified']} · "
        f"failed {c['pages_failed']}",
        f"  icons {c['icons_fetched']} · changed {c['icons_changed']} · failed {c['icons_failed']} · "
        f"vanished {len(result.vanished)} · unlisted releases {len(result.unlisted)}"
        f"{' · scope changed' if result.scope_changed else ''}",
    ])


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ul_house.crawl.run", description=__doc__.splitlines()[0])
    parser.add_argument("--seed", required=True, type=Path, help="the seed file")
    parser.add_argument("--init", action="store_true", help="create the seed if it does not exist")
    parser.add_argument("--resume", action="store_true", help="continue the active run")
    parser.add_argument("--limit", type=_positive, metavar="N", help="at most N detail-page requests")
    parser.add_argument("--base-url", default=BASE_URL, help="the site root")
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH, help="the scope file")
    parser.add_argument("--trigger", choices=RUN_TRIGGERS, default="manual")
    parser.add_argument("--code-commit", help="override the detected code commit")
    parser.add_argument("--catalog-commit", help="override the detected catalog commit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if not args.seed.exists():
        if not args.init:
            print(f"{args.seed}: no such seed (use --init to create it)", file=sys.stderr)
            return EXIT_REFUSED
        args.seed.parent.mkdir(parents=True, exist_ok=True)

    try:
        scope = load_scope(args.catalog)
    except ScopeError as exc:
        print(f"scope: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    lineage = Lineage.detect(args.catalog)
    lineage = replace(lineage, code_commit=args.code_commit or lineage.code_commit,
                      catalog_commit=args.catalog_commit or lineage.catalog_commit)

    stop = threading.Event()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
        except ValueError:
            break
    try:
        try:
            store = Store.open(args.seed)
        except SeedSchemaError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_REFUSED
        with store, Client(args.base_url, interval=scope.crawl.request_interval,
                           max_retries=scope.crawl.max_retries, timeout=scope.crawl.timeout,
                           deadline=scope.crawl.request_deadline, stop=stop) as client:
            try:
                result = crawl(client, store, scope, lineage=lineage, limit=args.limit,
                               resume=args.resume, trigger=args.trigger)
            except ResumeRefused as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return EXIT_REFUSED
            except CrawlFailed as exc:
                print(f"crawl failed (run {exc.run_id}): {exc}", file=sys.stderr)
                return EXIT_FAILED
            except Exception:
                traceback.print_exc()
                return EXIT_FAILED
        print(summary(result))
        return EXIT_STOPPED if result.stopped_by == "stop" else EXIT_OK
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
