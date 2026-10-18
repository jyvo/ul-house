from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ul_house.database import (
    applied_revision, entities, primary_key, quote_ident, table_columns,
)
from ul_house.sync.errors import (
    ApplyError, DatabaseBusy, PackageError, Refused, RevisionMismatch, SnapshotError, SyncError,
)
from ul_house.sync.protocol import (
    AliasOp, DeleteOp, IconDelta, Op, RetireOp, UpdatePackage, UpsertOp, format_utc, utc_now,
)


@dataclass(frozen=True)
class ApplyResult:
    revision: int
    from_revision: int | None
    kind: Literal["update", "snapshot"]
    counts: Mapping[str, int]               # {"UPSERT": n, ...} (snapshot: rows per table)
    icons: IconDelta | None = None


# transactions
@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("PRAGMA defer_foreign_keys = ON")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _stamp(now: Callable[[], datetime]) -> str:
    return format_utc(now())


def apply_package(conn: sqlite3.Connection, package: UpdatePackage, *, package_sha256: str,
                  app_version: str, now: Callable[[], datetime] = utc_now,
                  on_begin: Callable[[], None] | None = None) -> ApplyResult:
    """apply one verified update package in one transaction; revisions and versions are checked first.
    `on_begin` is called right before BEGIN IMMEDIATE (the UI warns before the write lock is taken)."""
    if package.dataset_schema_version != entities.DATASET_SCHEMA_VERSION:
        raise Refused("schema_unsupported",
                      f"package dataset schema {package.dataset_schema_version} is not supported: update the app")
    local = _check_revisions(applied_revision(conn), package)
    _check_package_invariants(package)
    counts: Mapping[str, int] = {}
    if on_begin is not None:
        on_begin()
    try:
        with transaction(conn):
            _check_revisions(applied_revision(conn), package)           # check if another process has synced
            counts = apply_ops(conn, package.ops)
            conn.execute(
                "INSERT INTO main.sync_history (revision, from_revision, kind, applied_at, app_version, "
                "package_sha256) VALUES (?, ?, 'update', ?, ?, ?)",
                (package.to_revision, local, _stamp(now), app_version, package_sha256))
    except SyncError:
        raise
    except sqlite3.OperationalError as exc:
        if _is_busy(exc):
            raise DatabaseBusy(f"database is busy: {exc}") from exc
        raise ApplyError(f"update {package.from_revision} -> {package.to_revision} failed and was rolled back: {exc}") from exc
    except Exception as exc:
        raise ApplyError(f"update {package.from_revision} -> {package.to_revision} failed and was rolled back: {exc}") from exc
    return ApplyResult(revision=package.to_revision, from_revision=local, kind="update",
                       counts=counts, icons=package.icons)


def _is_busy(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _check_revisions(local: int | None, package: UpdatePackage) -> int:
    if local is None:
        raise RevisionMismatch("no local revision: a package needs a snapshot first")
    if package.from_revision != local:
        raise RevisionMismatch(f"package starts at {package.from_revision} but the local revision is {local}")
    if package.to_revision <= local:
        raise RevisionMismatch(f"package leads to {package.to_revision}, not past the local revision {local}")
    return local


def _check_package_invariants(package: UpdatePackage) -> None:
    seen: set[tuple[str, str, str]] = set()
    for op in package.ops:
        if not package.from_revision < op.rev <= package.to_revision:
            raise PackageError(f"{op.op} {op.entity} {op.key}: rev {op.rev} is outside "
                               f"({package.from_revision}, {package.to_revision}]")
        ident = (op.op, op.entity, op.key)
        if ident in seen:
            raise PackageError(f"{op.op} {op.entity} {op.key} appears twice")
        seen.add(ident)
        if isinstance(op, AliasOp) and op.to == op.key:
            raise PackageError(f"ALIAS {op.key} points at itself")


# apply ops
class _Tables:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._columns: dict[str, tuple[str, ...]] = {}
        self._pk: dict[str, tuple[str, ...]] = {}

    def columns(self, table: str) -> tuple[str, ...]:
        if table not in self._columns:
            cols = table_columns(self._conn, table)
            if not cols:
                raise PackageError(f"table {table} does not exist locally")
            self._columns[table] = cols
        return self._columns[table]

    def pk(self, table: str) -> tuple[str, ...]:
        if table not in self._pk:
            self._pk[table] = primary_key(self._conn, table)
        return self._pk[table]


def apply_ops(conn: sqlite3.Connection, ops: Sequence[Op]) -> Mapping[str, int]:
    if not conn.in_transaction:
        raise ApplyError("apply_ops needs an open transaction")
    conn.execute("PRAGMA defer_foreign_keys = ON")
    tables = _Tables(conn)
    counts = {"UPSERT": 0, "RETIRE": 0, "ALIAS": 0, "DELETE": 0}
    retired: list[RetireOp] = []
    aliased: list[AliasOp] = []
    for op in ops:
        e, key = _resolve(op)
        if isinstance(op, UpsertOp):
            _upsert(conn, tables, e, key, op)
        elif isinstance(op, RetireOp):
            _retire(conn, e, op)
            retired.append(op)
        elif isinstance(op, AliasOp):
            _alias(conn, e, op)
            aliased.append(op)
        elif isinstance(op, DeleteOp):
            _delete(conn, e, key)
        else:
            raise PackageError(f"unknown op {op!r}")
        counts[op.op] += 1
    for op in retired:
        if conn.execute("SELECT 1 FROM main.uid_retired WHERE uid = ?", (op.key,)).fetchone() is None:
            raise PackageError(f"RETIRE {op.key} has no uid_retired ledger row")
    for op in aliased:
        row = conn.execute("SELECT new_uid, confirmed_commit FROM main.uid_alias WHERE old_uid = ?",
                           (op.key,)).fetchone()
        if row is None or row[0] != op.to or row[1] != op.confirmed_commit:
            raise PackageError(f"ALIAS {op.key} -> {op.to} does not match its uid_alias ledger row")
    return counts


def _resolve(op: Op) -> tuple[entities.Entity, int | str]:
    try:
        e = entities.entity(op.entity)
    except KeyError:
        raise PackageError(f"unknown entity {op.entity!r}") from None
    try:
        return e, entities.key_from_text(e, op.key)
    except (ValueError, TypeError) as exc:
        raise PackageError(f"{op.entity} key {op.key[:40]!r}: {exc}") from exc


def _params(e: entities.Entity, key: int | str) -> dict[str, Any]:
    return {"key": key, "key_text": entities.key_to_text(e, key)}


def _rows_of(payload: Any, table: str) -> list[Mapping[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, Mapping):
        return [payload]
    return list(payload)


def _check_row(tables: _Tables, table: str, row: Mapping[str, Any]) -> None:
    cols = tables.columns(table)
    if set(row) != set(cols):
        missing = sorted(set(cols) - set(row))
        extra = sorted(set(row) - set(cols))
        raise PackageError(f"{table} row must carry exactly its columns (missing {missing}, extra {extra})")


def _upsert(conn: sqlite3.Connection, tables: _Tables, e: entities.Entity, key: int | str, op: UpsertOp) -> None:
    root = e.root_table
    data = op.data
    owned_tables = {c.table for c in e.children}
    unknown = set(data) - owned_tables - {root}
    if unknown:
        raise PackageError(f"{op.entity} {op.key}: payload has tables the aggregate does not own: {sorted(unknown)}")
    root_payload = data.get(root)
    if not isinstance(root_payload, Mapping):
        raise PackageError(f"{op.entity} {op.key}: the root row {root} must be exactly one object")
    _check_row(tables, root, root_payload)
    if root_payload[e.key_columns[0]] != key or isinstance(root_payload[e.key_columns[0]], bool):
        raise PackageError(f"{op.entity} {op.key}: root row key {root_payload[e.key_columns[0]]!r} differs from the op key")

    link_kinds = {c.owner_kind for c in e.children if c.table == "effect_link"}
    assigned: list[list[Mapping[str, Any]]] = []
    for child in e.children:
        rows = _rows_of(data.get(child.table), child.table)
        if child.owner_kind is not None:
            rows = [r for r in rows if r.get("owner_kind") == child.owner_kind]
        assigned.append(rows)
    for r in _rows_of(data.get("effect_link"), "effect_link"):
        if r.get("owner_kind") not in link_kinds:
            raise PackageError(f"{op.entity} {op.key}: effect_link owner_kind {r.get('owner_kind')!r} is not owned by this aggregate")
    for child, rows in zip(e.children, assigned):
        for r in rows:
            _check_row(tables, child.table, r)

    if not e.children and not e.deletable and not e.retirable:
        _insert_ledger(conn, tables, e, key, root_payload)
        return

    params = _params(e, key)
    for child in e.children:
        conn.execute(f"DELETE FROM main.{quote_ident(child.table)} WHERE {child.predicate}", params)
    _upsert_root(conn, tables, root, root_payload)
    for child, rows in reversed(list(zip(e.children, assigned))):
        for r in rows:
            _insert(conn, tables, child.table, r)
    for child, rows in zip(e.children, assigned):
        (count,) = conn.execute(
            f"SELECT count(*) FROM main.{quote_ident(child.table)} WHERE {child.predicate}", params).fetchone()
        if count != len(rows):
            raise PackageError(
                f"{op.entity} {op.key}: {len(rows)} {child.table} rows in the payload but {count} belong to the aggregate")


def _upsert_root(conn: sqlite3.Connection, tables: _Tables, table: str, row: Mapping[str, Any]) -> None:
    cols = tables.columns(table)
    pk = tables.pk(table)
    non_pk = [c for c in cols if c not in pk]
    conflict = ", ".join(quote_ident(c) for c in pk)
    action = ("DO UPDATE SET " + ", ".join(f"{quote_ident(c)} = excluded.{quote_ident(c)}" for c in non_pk)
              if non_pk else "DO NOTHING")
    conn.execute(
        f"INSERT INTO main.{quote_ident(table)} ({', '.join(quote_ident(c) for c in cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)}) ON CONFLICT ({conflict}) {action}",
        [row[c] for c in cols])


def _insert(conn: sqlite3.Connection, tables: _Tables, table: str, row: Mapping[str, Any]) -> None:
    cols = tables.columns(table)
    conn.execute(
        f"INSERT INTO main.{quote_ident(table)} ({', '.join(quote_ident(c) for c in cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        [row[c] for c in cols])


def _insert_ledger(conn: sqlite3.Connection, tables: _Tables, e: entities.Entity, key: int | str, row: Mapping[str, Any]) -> None:
    # append only
    cols = tables.columns(e.root_table)
    pk = tables.pk(e.root_table)
    conn.execute(
        f"INSERT INTO main.{quote_ident(e.root_table)} ({', '.join(quote_ident(c) for c in cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)}) ON CONFLICT ({', '.join(quote_ident(c) for c in pk)}) DO NOTHING",
        [row[c] for c in cols])
    stored = conn.execute(
        f"SELECT {', '.join(quote_ident(c) for c in cols)} FROM main.{quote_ident(e.root_table)} "
        f"WHERE {quote_ident(e.key_columns[0])} = ?", (key,)).fetchone()
    if stored is None or tuple(stored) != tuple(row[c] for c in cols):
        raise PackageError(f"ledger row {e.entity_type} {key} already exists with different values (ledger rows never change)")


def _retire(conn: sqlite3.Connection, e: entities.Entity, op: RetireOp) -> None:
    if not e.retirable:
        raise PackageError(f"{op.entity} cannot be retired")
    cur = conn.execute(
        "UPDATE main.equipment SET state = 'retired', retired_revision = :rev, last_changed_revision = :rev "
        "WHERE uid = :key", {"rev": op.rev, "key": op.key})
    if cur.rowcount != 1:
        raise PackageError(f"RETIRE {op.key}: no such equipment row")


def _alias(conn: sqlite3.Connection, e: entities.Entity, op: AliasOp) -> None:
    if not e.aliasable:
        raise PackageError(f"{op.entity} cannot be aliased")
    repoint_uid(conn, op.key, op.to)


def _delete(conn: sqlite3.Connection, e: entities.Entity, key: int | str) -> None:
    if not e.deletable:
        raise PackageError(f"{e.entity_type} is never deleted")
    params = _params(e, key)
    for child in e.children:
        conn.execute(f"DELETE FROM main.{quote_ident(child.table)} WHERE {child.predicate}", params)
    where, args = entities.root_where(e, key)
    conn.execute(f"DELETE FROM main.{quote_ident(e.root_table)} WHERE {where}", args)


# user tables
def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = ?",
                        (table,)).fetchone() is not None


def drop_saved_refs_shadowed_by_alias(conn: sqlite3.Connection, old_uids_sql: str, params: Sequence[Any] = ()) -> int:
    if not _has_table(conn, "saved_ref"):
        return 0
    before = conn.total_changes
    conn.execute(
        "WITH pairs(old_uid, new_uid) AS (" + old_uids_sql + ") "
        "DELETE FROM main.saved_ref WHERE EXISTS ("
        "SELECT 1 FROM pairs p WHERE p.old_uid = saved_ref.uid AND ("
        "EXISTS (SELECT 1 FROM main.saved_ref s2 WHERE s2.profile_id = saved_ref.profile_id AND s2.uid = p.new_uid) "
        "OR EXISTS (SELECT 1 FROM pairs p2 JOIN main.saved_ref s3 ON s3.uid = p2.old_uid "
        "WHERE p2.new_uid = p.new_uid AND p2.old_uid < p.old_uid AND s3.profile_id = saved_ref.profile_id)))",
        tuple(params))
    return conn.total_changes - before


def repoint_uid(conn: sqlite3.Connection, old: str, new: str) -> int:
    changed = 0
    if _has_table(conn, "saved_ref"):
        drop_saved_refs_shadowed_by_alias(conn, "SELECT ?, ?", (old, new))
        changed += conn.execute("UPDATE main.saved_ref SET uid = :new WHERE uid = :old",
                                {"new": new, "old": old}).rowcount
    if _has_table(conn, "inventory"):
        changed += conn.execute("UPDATE main.inventory SET uid = :new WHERE uid = :old",
                                {"new": new, "old": old}).rowcount
    return changed


_MAX_ALIAS_DEPTH = 64


def resolve_user_aliases(conn: sqlite3.Connection) -> int:
    if not conn.in_transaction:
        raise SnapshotError("resolve_user_aliases needs the snapshot transaction")
    conn.execute("DROP TABLE IF EXISTS temp.alias_end")
    conn.execute(
        "CREATE TEMP TABLE alias_end AS "
        "WITH RECURSIVE walk(start, cur, depth) AS ("
        "SELECT old_uid, new_uid, 1 FROM main.uid_alias "
        "UNION ALL "
        "SELECT w.start, a.new_uid, w.depth + 1 FROM walk w JOIN main.uid_alias a ON a.old_uid = w.cur "
        f"WHERE w.depth < {_MAX_ALIAS_DEPTH}) "
        "SELECT start, cur FROM walk w "
        "WHERE NOT EXISTS (SELECT 1 FROM main.uid_alias a WHERE a.old_uid = w.cur)")
    try:
        looped = conn.execute(
            "SELECT old_uid FROM main.uid_alias WHERE old_uid NOT IN (SELECT start FROM temp.alias_end) LIMIT 1"
        ).fetchone()
        if looped is not None:
            raise SnapshotError(f"the alias ledger has a cycle (or a chain deeper than {_MAX_ALIAS_DEPTH}) at {looped[0]}")
        not_live = "SELECT uid FROM main.equipment WHERE state = 'live'"
        pairs = f"SELECT start, cur FROM temp.alias_end WHERE start NOT IN ({not_live})"
        changed = 0
        if _has_table(conn, "saved_ref"):
            drop_saved_refs_shadowed_by_alias(conn, pairs)
            changed += conn.execute(
                "UPDATE main.saved_ref SET uid = (SELECT cur FROM temp.alias_end WHERE start = saved_ref.uid) "
                f"WHERE uid IN (SELECT start FROM temp.alias_end) AND uid NOT IN ({not_live})").rowcount
        if _has_table(conn, "inventory"):
            changed += conn.execute(
                "UPDATE main.inventory SET uid = (SELECT cur FROM temp.alias_end WHERE start = inventory.uid) "
                f"WHERE uid IN (SELECT start FROM temp.alias_end) AND uid NOT IN ({not_live})").rowcount
        return changed
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.alias_end")
