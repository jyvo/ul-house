from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ul_house.seed.store import Store

SOURCES = ("list", "new_release", "evolution", "manual")
ENTRY_KINDS = ("catalog", "reference")


@dataclass(frozen=True)
class Entry:
    item_id: str
    source: str
    entry_kind: str
    keep_reason: str
    depth: int = 0
    parent_id: str | None = None
    state: str = "pending"
    attempts: int = 0
    last_error: str | None = None

    def strength(self) -> tuple[bool, int]:
        return (self.entry_kind == "catalog", -self.depth)


def merge(current: Entry, reached: Entry) -> Entry:
    kind = "catalog" if "catalog" in (current.entry_kind, reached.entry_kind) else "reference"
    path = reached if reached.depth < current.depth else current
    return Entry(current.item_id, path.source, kind, path.keep_reason, path.depth, path.parent_id,
                 current.state, current.attempts, current.last_error)


class Frontier:
    _COLUMNS = "item_id, source, entry_kind, keep_reason, depth, parent_id, state, attempts, last_error"

    def __init__(self, store: Store, max_attempts: int):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.store = store
        self.max_attempts = max_attempts

    @property
    def _conn(self):
        return self.store.conn

    def get(self, item_id: str) -> Entry | None:
        row = self._conn.execute(
            "SELECT item_id, source, entry_kind, keep_reason, depth, parent_id, state, attempts, last_error "
            "FROM frontier WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return Entry(**dict(row)) if row else None

    def decided_run(self, item_id: str) -> int | None:
        row = self._conn.execute("SELECT decided_run_id FROM frontier WHERE item_id = ?", (item_id,)).fetchone()
        return row["decided_run_id"] if row else None

    def enqueue(self, entry: Entry, run_id: int | None = None) -> str:
        """insert, re-decide or strengthen"""
        if entry.source not in SOURCES:
            raise ValueError(f"unknown source {entry.source!r}")
        if entry.entry_kind not in ENTRY_KINDS:
            raise ValueError(f"unknown entry kind {entry.entry_kind!r}")
        now = self.store.clock()
        current = self.get(entry.item_id)

        if current is None:
            self._conn.execute(
                "INSERT INTO frontier (item_id, source, entry_kind, keep_reason, depth, parent_id, "
                "state, attempts, discovered_at, updated_at, decided_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
                (entry.item_id, entry.source, entry.entry_kind, entry.keep_reason, entry.depth,
                 entry.parent_id, now, now, run_id),
            )
            return "new"

        if run_id is not None and self.decided_run(entry.item_id) != run_id:
            self._conn.execute(
                "UPDATE frontier SET source = ?, entry_kind = ?, keep_reason = ?, depth = ?, parent_id = ?, "
                "decided_run_id = ?, updated_at = ? WHERE item_id = ?",
                (entry.source, entry.entry_kind, entry.keep_reason, entry.depth, entry.parent_id, run_id,
                 now, entry.item_id),
            )
            return "redecided"

        merged = merge(current, entry)
        if merged == current:
            return "kept"

        self._conn.execute(
            "UPDATE frontier SET source = ?, entry_kind = ?, keep_reason = ?, depth = ?, parent_id = ?, "
            "updated_at = ? WHERE item_id = ?",
            (merged.source, merged.entry_kind, merged.keep_reason, merged.depth, merged.parent_id, now,
             merged.item_id),
        )
        return "upgraded"

    def enqueue_all(self, entries: Iterable[Entry], run_id: int | None = None) -> dict[str, int]:
        counts = {"new": 0, "upgraded": 0, "kept": 0, "redecided": 0}
        with self.store.transaction():
            for entry in entries:
                counts[self.enqueue(entry, run_id)] += 1
        return counts

    def reset_in_flight(self) -> int:
        cursor = self._conn.execute(
            "UPDATE frontier SET state = 'pending', attempts = MAX(attempts - 1, 0), updated_at = ? "
            "WHERE state = 'in_flight'",
            (self.store.clock(),),
        )
        return cursor.rowcount

    def claim_next(self, run_id: int | None = None) -> Entry | None:
        where, args = ("AND decided_run_id = ?", (run_id,)) if run_id is not None else ("", ())
        with self.store.transaction():
            row = self._conn.execute(
                f"SELECT item_id FROM frontier WHERE state = 'pending' {where} ORDER BY depth, item_id LIMIT 1",
                args,
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE frontier SET state = 'in_flight', attempts = attempts + 1, updated_at = ? "
                "WHERE item_id = ?",
                (self.store.clock(), row["item_id"]),
            )
            return self.get(row["item_id"])

    def _set_state(self, item_id: str, state: str, error: str | None = None) -> None:
        cursor = self._conn.execute(
            "UPDATE frontier SET state = ?, last_error = ?, updated_at = ? WHERE item_id = ? AND state = 'in_flight'",
            (state, error, self.store.clock(), item_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"{item_id} is not in flight")

    def mark_done(self, item_id: str) -> None:
        self._set_state(item_id, "done")

    def mark_gone(self, item_id: str, error: str = "404") -> None:
        self._set_state(item_id, "gone", error)

    def mark_blocked(self, item_id: str, error: str) -> None:
        """in flight -> failed at once, whatever attempts remain (contract failure, robots)"""
        self._set_state(item_id, "failed", error)

    def revalidated(self, item_id: str, state: str, error: str | None = None) -> None:
        if state not in ("done", "gone"):
            raise ValueError(f"a revalidated id is done or gone, not {state!r}")
        cursor = self._conn.execute(
            "UPDATE frontier SET state = ?, last_error = ?, updated_at = ? "
            "WHERE item_id = ? AND state IN ('done', 'gone')",
            (state, error, self.store.clock(), item_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"{item_id} is not done or gone")

    def retry_failed(self) -> int:
        cursor = self._conn.execute(
            "UPDATE frontier SET state = 'pending', attempts = 0, updated_at = ? WHERE state = 'failed'",
            (self.store.clock(),),
        )
        return cursor.rowcount

    def pending_count(self, run_id: int | None = None) -> int:
        where, args = ("AND decided_run_id = ?", (run_id,)) if run_id is not None else ("", ())
        return self._conn.execute(
            f"SELECT count(*) FROM frontier WHERE state = 'pending' {where}", args).fetchone()[0]

    def mark_failed(self, item_id: str, error: str) -> str:
        """back to pending while attempts remain"""
        entry = self.get(item_id)
        state = "failed" if entry is not None and entry.attempts >= self.max_attempts else "pending"
        self._set_state(item_id, state, error)
        return state

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT state, count(*) AS n FROM frontier GROUP BY state").fetchall()
        return {row["state"]: row["n"] for row in rows}

    def at_depth(self, depth: int, state: str = "done", run_id: int | None = None) -> list[Entry]:
        where, args = ("AND decided_run_id = ?", (run_id,)) if run_id is not None else ("", ())
        rows = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM frontier WHERE depth = ? AND state = ? {where} ORDER BY item_id",
            (depth, state, *args),
        ).fetchall()
        return [Entry(**dict(row)) for row in rows]

    def decided(self, run_id: int, states: tuple[str, ...] = ("done", "gone")) -> list[Entry]:
        """ids decided in this run in states"""
        marks = ",".join("?" * len(states))
        rows = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM frontier WHERE decided_run_id = ? AND state IN ({marks}) "
            "ORDER BY item_id",
            (run_id, *states),
        ).fetchall()
        return [Entry(**dict(row)) for row in rows]
