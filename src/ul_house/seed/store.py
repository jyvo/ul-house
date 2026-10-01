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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


class Store:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], str] = utc_now):
        self.conn = conn
        self.clock = clock
        self._depth = 0

    @classmethod
    def open(cls, path: str | Path, clock: Callable[[], str] = utc_now) -> Store:
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_PATH.read_text())
        return cls(conn, clock)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._depth:
            self._depth += 1
            try:
                yield
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
            self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    def record_response(self, url: str, kind: str, status: int, body: bytes | None = None, etag: str | None = None, 
                        last_modified: str | None = None, content_type: str | None = None, item_id: str | None = None) -> bool:
        """returns True when the stored html changed
            - 200 > replaces html
            - 304 > keeps it
            - anything else > records status and keep html that was already there
        """
        if kind not in PAGE_KINDS:
            raise ValueError(f"unknown page kind {kind!r}")
        now = self.clock()
        old = self.conn.execute(
            "SELECT html_sha256, changed_at, etag, last_modified FROM page WHERE url = ?", (url,)
        ).fetchone()

        if status == 200:
            if body is None:
                raise ValueError("a 200 needs a body")
            sha = hashlib.sha256(body).hexdigest()
            changed = old is None or old["html_sha256"] != sha
            self.conn.execute(
                """
                INSERT INTO page (url, kind, item_id, html, html_sha256, etag, last_modified,
                                  status, content_type, fetched_at, changed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 200, ?, ?, ?)
                ON CONFLICT (url) DO UPDATE SET
                    html = excluded.html, html_sha256 = excluded.html_sha256,
                    etag = excluded.etag, last_modified = excluded.last_modified,
                    status = 200, content_type = excluded.content_type,
                    fetched_at = excluded.fetched_at, changed_at = excluded.changed_at
                """,
                (url, kind, item_id, zlib.compress(body, COMPRESS_LEVEL), sha, etag, last_modified,
                 content_type, now, now if changed else old["changed_at"]),
            )
            return changed

        if status == 304:
            if old is None or old["html_sha256"] is None:
                raise ValueError(f"304 for {url} but no stored html to keep")
            # 304 may refresh validators (unchanged not cleared)
            self.conn.execute(
                "UPDATE page SET status = 304, fetched_at = ?, etag = ?, last_modified = ? WHERE url = ?",
                (now, etag or old["etag"], last_modified or old["last_modified"], url),
            )
            return False

        self.conn.execute(
            """
            INSERT INTO page (url, kind, item_id, status, content_type, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (url) DO UPDATE SET status = excluded.status, fetched_at = excluded.fetched_at
            """,
            (url, kind, item_id, status, content_type, now),
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
            "fetched_at, changed_at FROM page WHERE url = ?",
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
