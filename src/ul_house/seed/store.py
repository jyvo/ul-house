"""seed.sqlite
    - bytes + validators
    - list rows discovery saw
    - lineage links read off detail pages
    - autocommit mode > transaction() groups writes while crawl commits a page with frontier transaction
"""
from __future__ import annotations 

import hashlib
import sqlite3
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
COMPRESS_LEVEL = 6
PAGE_KINDS = ("list", "new_release", "detail")
SCHEMA_VERSION = 1
ASSET_KINDS = ("equipment", "item", "ability")
_ASSET_COLUMNS = ("url, item_id, kind, sha256, etag, last_modified, status, content_type, fetched_at, changed_at, run_id, changed_run_id")
RUN_STATUSES = ("running", "complete", "partial", "failed", "abandoned")
RUN_TRIGGERS = ("schedule", "dispatch", "manual")
RUN_COUNTERS = ("pages_discovered", "pages_fetched", "pages_changed", "pages_failed", "pages_not_modified",
                "icons_fetched", "icons_changed", "icons_failed", "shard_items", "new_release_skipped")


class SeedSchemaError(RuntimeError):
    """file is a seed from before the stamped schema; it cannot be opened, only rebuilt"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Validators:
    etag: str | None
    last_modified: str | None

    def headers(self) -> dict[str, str]:
        """conditional-request headers (empty if no validators)"""
        headers = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers


class ListRowLike(Protocol):
    item_id: str
    source_url: str
    name: str
    grp: str
    rarity: str
    gear_type: str | None
    element: str
    cost: int


class LinkLike(Protocol):
    source_id: str
    kind: str
    side: str
    target_id: str
    target_name: str


class AssetTargetLike(Protocol):
    url: str
    item_id: str
    kind: str


class ReleaseRowLike(Protocol):
    item_id: str
    name: str


class Store:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], str] = utc_now):
        self.conn = conn
        self.clock = clock
        self._depth = 0

    @classmethod
    def open(cls, path: str | Path, clock: Callable[[], str] = utc_now) -> Store:
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            cls._check_schema_version(conn, path)
            conn.executescript(SCHEMA_PATH.read_text())
        except BaseException:
            conn.close()
            raise
        return cls(conn, clock)

    @staticmethod
    def _check_schema_version(conn: sqlite3.Connection, path: str | Path) -> None:
        """rebuild an old seed (old schema)"""
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "page" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(page)")}
            if "run_id" not in columns:
                raise SeedSchemaError(f"{path}: seed predates schema {SCHEMA_VERSION}; rebuild it")
        if "meta" in tables:
            row = conn.execute("SELECT value FROM meta WHERE key = 'seed.schema_version'").fetchone()
            if row is not None and row[0] != str(SCHEMA_VERSION):
                raise SeedSchemaError(f"{path}: seed schema {row[0]}, this code needs {SCHEMA_VERSION}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._depth:
            savepoint = f"sp_{self._depth}"
            self.conn.execute(f"SAVEPOINT {savepoint}")
            self._depth += 1
            try:
                yield
            except BaseException:
                self.conn.execute(f"ROLLBACK TO {savepoint}")
                self.conn.execute(f"RELEASE {savepoint}")
                raise
            else:
                self.conn.execute(f"RELEASE {savepoint}")
            finally:
                self._depth -= 1
            return

        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            try:
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
        finally:
            self._depth = 0

    def record_response(self, url: str, kind: str, status: int, body: bytes | None = None, etag: str | None = None, last_modified: str | None = None,
                        content_type: str | None = None, item_id: str | None = None, run_id: int | None = None) -> bool:
        """returns True when the stored html changed
            - 200 > replaces html
            - 304 > keeps it
            - anything else > records status and keep html that was already there
        """
        if kind not in PAGE_KINDS:
            raise ValueError(f"unknown page kind {kind!r}")
        now = self.clock()
        old = self.conn.execute("SELECT html_sha256, changed_at, changed_run_id, etag, last_modified FROM page WHERE url = ?", (url,)).fetchone()

        if status == 200:
            if body is None:
                raise ValueError("a 200 needs a body")
            sha = hashlib.sha256(body).hexdigest()
            changed = old is None or old["html_sha256"] != sha
            self.conn.execute(
                """
                INSERT INTO page (url, kind, item_id, html, html_sha256, etag, last_modified,
                                  status, content_type, fetched_at, changed_at, run_id, changed_run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, 200, ?, ?, ?, ?, ?)
                ON CONFLICT (url) DO UPDATE SET
                    html = excluded.html, html_sha256 = excluded.html_sha256,
                    etag = excluded.etag, last_modified = excluded.last_modified,
                    status = 200, content_type = excluded.content_type,
                    fetched_at = excluded.fetched_at, changed_at = excluded.changed_at,
                    run_id = excluded.run_id, changed_run_id = excluded.changed_run_id
                """,
                (url, kind, item_id, zlib.compress(body, COMPRESS_LEVEL), sha, etag, last_modified,
                 content_type, now, now if changed else old["changed_at"], run_id,
                 run_id if changed else (old["changed_run_id"] if old else None)),
            )
            return changed

        if status == 304:
            if old is None or old["html_sha256"] is None:
                raise ValueError(f"304 for {url} but no stored html to keep")
            # 304 may refresh validators (unchanged not cleared)
            self.conn.execute(
                "UPDATE page SET status = 304, fetched_at = ?, etag = ?, last_modified = ?, run_id = ? WHERE url = ?",
                (now, etag or old["etag"], last_modified or old["last_modified"], run_id, url),
            )
            return False

        self.conn.execute(
            """
            INSERT INTO page (url, kind, item_id, status, content_type, fetched_at, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (url) DO UPDATE SET status = excluded.status, fetched_at = excluded.fetched_at, run_id = excluded.run_id
            """,
            (url, kind, item_id, status, content_type, now, run_id),
        )
        return False

    def validators(self, url: str) -> Validators | None:
        """fall back, None unless html is stored"""
        row = self.conn.execute(
            "SELECT etag, last_modified FROM page WHERE url = ? AND html IS NOT NULL", (url,)
        ).fetchone()
        return Validators(row["etag"], row["last_modified"]) if row else None

    def html(self, url: str) -> str | None:
        row = self.conn.execute("SELECT html FROM page WHERE url = ?", (url,)).fetchone()
        if row is None or row["html"] is None:
            return None
        return zlib.decompress(row["html"]).decode("utf-8", errors="replace")

    def page(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT url, kind, item_id, html_sha256, etag, last_modified, status, content_type, "
            "fetched_at, changed_at, run_id, changed_run_id FROM page WHERE url = ?",
            (url,),
        ).fetchone()

    def put_listing(self, rows: Iterable[ListRowLike]) -> int:
        now = self.clock()
        cursor = self.conn.executemany(
            """
            INSERT INTO listing (item_id, source_url, name, grp, rarity, gear_type, element, cost, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (item_id) DO UPDATE SET
                source_url = excluded.source_url, name = excluded.name, grp = excluded.grp,
                rarity = excluded.rarity, gear_type = excluded.gear_type,
                element = excluded.element, cost = excluded.cost, seen_at = excluded.seen_at
            """,
            [(r.item_id, r.source_url, r.name, r.grp, r.rarity, r.gear_type, r.element, r.cost, now) for r in rows],
        )
        return cursor.rowcount

    def listing(self, item_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM listing WHERE item_id = ?", (item_id,)).fetchone()

    def listing_all(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM listing ORDER BY item_id").fetchall()

    def put_links(self, source_id: str, links: Iterable[LinkLike]) -> None:
        """replace everything a page said about its lineage"""
        with self.transaction():
            self.conn.execute("DELETE FROM evo_link WHERE source_id = ?", (source_id,))
            self.conn.executemany(
                "INSERT INTO evo_link (source_id, kind, side, target_id, target_name) VALUES (?, ?, ?, ?, ?)",
                [(source_id, l.kind, l.side, l.target_id, l.target_name) for l in links],
            )

    def links_from(self, source_ids: Iterable[str]) -> list[sqlite3.Row]:
        ids = list(source_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self.conn.execute(
            f"SELECT * FROM evo_link WHERE source_id IN ({marks}) ORDER BY source_id, kind, side", ids
        ).fetchall()

    def links_to(self, target_ids: Iterable[str]) -> list[sqlite3.Row]:
        ids = list(target_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self.conn.execute(
            f"SELECT * FROM evo_link WHERE target_id IN ({marks}) ORDER BY target_id, kind, source_id", ids
        ).fetchall()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete_meta(self, key: str) -> None:
        self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    def start_run(self, *, trigger: str, code_commit: str, catalog_version: str, catalog_commit: str,
                  shard_index: int | None, code_dirty: bool = False) -> int:
        if trigger not in RUN_TRIGGERS:
            raise ValueError(f"unknown trigger {trigger!r}")
        cursor = self.conn.execute(
            "INSERT INTO crawl_run (trigger, started_at, code_commit, catalog_version, catalog_commit, "
            "status, shard_index, code_dirty) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
            (trigger, self.clock(), code_commit, catalog_version, catalog_commit, shard_index,
             1 if code_dirty else 0),
        )
        return cursor.lastrowid

    def bump_run(self, run_id: int, **deltas: int) -> None:
        for name in deltas:
            if name not in RUN_COUNTERS:
                raise ValueError(f"unknown run counter {name!r}")
        live = {name: delta for name, delta in deltas.items() if delta}
        if not live:
            if self.run(run_id) is None:
                raise ValueError(f"no run {run_id}")
            return
        assignments = ", ".join(f"{name} = {name} + ?" for name in live)
        cursor = self.conn.execute(
            f"UPDATE crawl_run SET {assignments} WHERE run_id = ?", (*live.values(), run_id))
        if cursor.rowcount != 1:
            raise ValueError(f"no run {run_id}")

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        if status not in RUN_STATUSES or status == "running":
            raise ValueError(f"cannot finish a run as {status!r}")
        ended = None if status == "abandoned" else self.clock()
        cursor = self.conn.execute(
            "UPDATE crawl_run SET status = ?, ended_at = ?, error = ? WHERE run_id = ?",
            (status, ended, error, run_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"no run {run_id}")

    def reopen_run(self, run_id: int) -> None:
        cursor = self.conn.execute(
            "UPDATE crawl_run SET status = 'running', ended_at = NULL WHERE run_id = ?", (run_id,))
        if cursor.rowcount != 1:
            raise ValueError(f"no run {run_id}")

    def run(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM crawl_run WHERE run_id = ?", (run_id,)).fetchone()

    def runs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM crawl_run ORDER BY run_id").fetchall()

    def put_asset_targets(self, item_id: str, targets: Iterable[AssetTargetLike]) -> None:
        targets = list(targets)
        for target in targets:
            if target.kind not in ASSET_KINDS:
                raise ValueError(f"unknown asset kind {target.kind!r}")
        with self.transaction():
            self.conn.executemany(
                "INSERT INTO asset (url, item_id, kind) VALUES (?, ?, ?) "
                "ON CONFLICT (url) DO UPDATE SET item_id = excluded.item_id, kind = excluded.kind",
                [(t.url, item_id, t.kind) for t in targets],
            )
            urls = sorted({t.url for t in targets})
            marks = ",".join("?" * len(urls))
            self.conn.execute(
                f"DELETE FROM asset WHERE item_id = ? AND url NOT IN ({marks})" if urls
                else "DELETE FROM asset WHERE item_id = ?",
                (item_id, *urls),
            )

    def record_asset(self, url: str, status: int, body: bytes | None = None, etag: str | None = None,
                     last_modified: str | None = None, content_type: str | None = None,
                     run_id: int | None = None) -> bool:
        """True when stored bytes changed. 200 replaces them, 304 keeps them"""
        old = self.conn.execute("SELECT sha256, etag, last_modified, changed_at, changed_run_id FROM asset WHERE url = ?", (url,)).fetchone()
        if old is None:
            raise ValueError(f"no asset target {url}")
        now = self.clock()

        if status == 200:
            if body is None:
                raise ValueError("a 200 needs a body")
            sha = hashlib.sha256(body).hexdigest()
            changed = old["sha256"] != sha
            self.conn.execute(
                "UPDATE asset SET body = ?, sha256 = ?, etag = ?, last_modified = ?, status = 200, "
                "content_type = ?, fetched_at = ?, changed_at = ?, run_id = ?, changed_run_id = ? WHERE url = ?",
                (body, sha, etag, last_modified, content_type, now,
                 now if changed else old["changed_at"], run_id,
                 run_id if changed else old["changed_run_id"], url),
            )
            return changed

        if status == 304:
            if old["sha256"] is None:
                raise ValueError(f"304 for {url} but no stored bytes to keep")
            self.conn.execute(
                "UPDATE asset SET status = 304, fetched_at = ?, etag = ?, last_modified = ?, run_id = ? "
                "WHERE url = ?",
                (now, etag or old["etag"], last_modified or old["last_modified"], run_id, url),
            )
            return False

        raise ValueError(f"record_asset takes 200 or 304, not {status}; use record_asset_status")

    def record_asset_status(self, url: str, status: int, run_id: int | None = None) -> None:
        """response bytes not accepted, status and time only (stored bytes left untouched)"""
        cursor = self.conn.execute(
            "UPDATE asset SET status = ?, fetched_at = ?, run_id = ? WHERE url = ?",
            (status, self.clock(), run_id, url),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"no asset target {url}")

    def asset(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute(f"SELECT {self._ASSET_COLUMNS} FROM asset WHERE url = ?", (url,)).fetchone()

    def asset_body(self, url: str) -> bytes | None:
        row = self.conn.execute("SELECT body FROM asset WHERE url = ?", (url,)).fetchone()
        return bytes(row["body"]) if row is not None and row["body"] is not None else None

    def asset_validators(self, url: str) -> Validators | None:
        """None unless bytes are stored"""
        row = self.conn.execute(
            "SELECT etag, last_modified FROM asset WHERE url = ? AND body IS NOT NULL", (url,)
        ).fetchone()
        return Validators(row["etag"], row["last_modified"]) if row else None

    def assets_for(self, item_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(f"SELECT {self._ASSET_COLUMNS} FROM asset WHERE item_id = ? ORDER BY url", (item_id,)).fetchall()

    def unfetched_assets(self, run_id: int) -> list[sqlite3.Row]:
        columns = ", ".join(f"a.{c.strip()}" for c in self._ASSET_COLUMNS.split(","))
        return self.conn.execute(
            f"SELECT {columns} FROM asset a JOIN frontier f ON f.item_id = a.item_id "
            "WHERE a.fetched_at IS NULL AND f.decided_run_id = ? ORDER BY a.url",
            (run_id,),
        ).fetchall()

    def put_unlisted(self, run_id: int, rows: Iterable[ReleaseRowLike]) -> None:
        """track how many consecutive runs each new release id not on list page"""
        rows = list(rows)
        with self.transaction():
            for row in rows:
                current = self.conn.execute(
                    "SELECT last_run_id FROM unlisted_release WHERE item_id = ?", (row.item_id,)
                ).fetchone()
                if current is None:
                    self.conn.execute(
                        "INSERT INTO unlisted_release (item_id, name, first_run_id, last_run_id, runs) "
                        "VALUES (?, ?, ?, ?, 1)", (row.item_id, row.name, run_id, run_id))
                elif current["last_run_id"] != run_id:
                    self.conn.execute(
                        "UPDATE unlisted_release SET name = ?, last_run_id = ?, runs = runs + 1 "
                        "WHERE item_id = ?", (row.name, run_id, row.item_id))
            ids = sorted({row.item_id for row in rows})
            marks = ",".join("?" * len(ids))
            self.conn.execute(
                f"DELETE FROM unlisted_release WHERE item_id NOT IN ({marks})" if ids
                else "DELETE FROM unlisted_release",
                ids,
            )

    def unlisted(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM unlisted_release ORDER BY item_id").fetchall()
