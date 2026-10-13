from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from ul_house.database import applied_revision, game_tables, quote_ident, table_columns, table_decl
from ul_house.sync import apply
from ul_house.sync.apply import ApplyResult, transaction
from ul_house.sync.errors import RevisionMismatch, SnapshotError, SyncError
from ul_house.sync.protocol import format_utc, utc_now
from ul_house.sync.verify import MAX_SNAPSHOT_BYTES, gunzip_to_file, require_sha256, sha256_hex

SNAP = "snap"


def expected_layout(conn: sqlite3.Connection) -> dict[str, tuple[tuple[str, str], ...]]:
    """table -> ((column, declared type), ...) of the client's game + ledger tables"""
    return {t: table_decl(conn, t) for t in game_tables(conn)}


def validate_snapshot(path: Path, expected: Mapping[str, tuple], entity_counts: Mapping[str, int] | None = None) -> None:
    """check a snapshot file on a throw-away read-only connection; SnapshotError on any failure"""
    path = Path(path)
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        v = sqlite3.connect(uri, uri=True, isolation_level=None)
    except (sqlite3.Error, OSError) as exc:
        raise SnapshotError(f"snapshot cannot be opened: {exc}") from exc
    try:
        v.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        v.execute("PRAGMA trusted_schema = OFF")
        v.execute("PRAGMA cell_size_check = ON")
        v.execute("PRAGMA mmap_size = 0")
        check = v.execute("PRAGMA quick_check").fetchall()
        if check != [("ok",)]:
            raise SnapshotError(f"snapshot failed quick_check: {check[:3]}")
        mode = v.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() == "wal":
            raise SnapshotError("snapshot is in WAL mode (journal_mode=DELETE is required, P15)")
        master = v.execute("SELECT type, name FROM sqlite_master").fetchall()
        extras = sorted(name for kind, name in master if kind in ("view", "trigger"))
        if extras:
            raise SnapshotError(f"snapshot holds views or triggers: {extras}")
        tables = {name for kind, name in master if kind == "table" and not name.startswith("sqlite_")}
        if tables != set(expected):
            raise SnapshotError(
                f"snapshot tables differ from the client's (missing {sorted(set(expected) - tables)}, "
                f"extra {sorted(tables - set(expected))})")
        for table, want in expected.items():
            got = [(row[1], (row[2] or "").upper()) for row in
                   v.execute(f"PRAGMA table_info({quote_ident(table)})")]
            want_names = [w if isinstance(w, str) else w[0] for w in want]
            if [g[0] for g in got] != want_names:
                raise SnapshotError(f"snapshot table {table} has columns {[g[0] for g in got]}, expected {want_names}")
            if want and not isinstance(want[0], str):
                want_types = [(w[0], (w[1] or "").upper()) for w in want]
                if got != want_types:
                    raise SnapshotError(f"snapshot table {table} has column types {got}, expected {want_types}")
        if entity_counts is not None:
            for table in expected:
                if table in entity_counts:
                    (n,) = v.execute(f"SELECT count(*) FROM {quote_ident(table)}").fetchone()
                    if n != entity_counts[table]:
                        raise SnapshotError(f"snapshot table {table} has {n} rows, the manifest says {entity_counts[table]}")
    except SnapshotError:
        raise
    except sqlite3.Error as exc:
        raise SnapshotError(f"snapshot is not a usable SQLite file: {exc}") from exc
    finally:
        v.close()


def _attached(conn: sqlite3.Connection) -> bool:
    return any(row[1] == SNAP for row in conn.execute("PRAGMA database_list"))


def apply_snapshot(conn: sqlite3.Connection, path: Path, *, revision: int, package_sha256: str,
                   app_version: str, repair: bool = False,
                   now: Callable[[], datetime] = utc_now,
                   on_begin: Callable[[], None] | None = None) -> ApplyResult:
    """replace the game and ledger tables from a validated snapshot file in one transaction"""
    local = applied_revision(conn)
    _check_revision(local, revision, repair)
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    tables = game_tables(conn)
    counts: dict[str, int] = {}
    attached = False
    try:
        if _attached(conn):
            raise SnapshotError("a database is already attached as snap")
        conn.execute(f"ATTACH DATABASE ? AS {SNAP}", (uri,))
        attached = True
        if on_begin is not None:
            on_begin()
        with transaction(conn):
            _check_revision(applied_revision(conn), revision, repair)
            for table in reversed(tables):                                  # children first
                conn.execute(f"DELETE FROM main.{quote_ident(table)}")
            for table in tables:                                            # parents first
                cols = ", ".join(quote_ident(c) for c in table_columns(conn, table))
                counts[table] = conn.execute(
                    f"INSERT INTO main.{quote_ident(table)} ({cols}) SELECT {cols} FROM {SNAP}.{quote_ident(table)}"
                ).rowcount
            apply.resolve_user_aliases(conn)
            stamp = format_utc(now())
            if repair:
                conn.execute(
                    "INSERT INTO main.sync_history (revision, from_revision, kind, applied_at, app_version, "
                    "package_sha256) VALUES (?, ?, 'snapshot', ?, ?, ?) ON CONFLICT (revision) DO UPDATE SET "
                    "kind = 'snapshot', applied_at = excluded.applied_at, app_version = excluded.app_version, "
                    "package_sha256 = excluded.package_sha256",
                    (revision, local, stamp, app_version, package_sha256))
            else:
                conn.execute(
                    "INSERT INTO main.sync_history (revision, from_revision, kind, applied_at, app_version, "
                    "package_sha256) VALUES (?, ?, 'snapshot', ?, ?, ?)",
                    (revision, local, stamp, app_version, package_sha256))
    except SyncError:
        raise
    except Exception as exc:
        raise SnapshotError(f"snapshot {revision} failed and was rolled back: {exc}") from exc
    finally:
        if attached and not conn.in_transaction:
            try:
                conn.execute(f"DETACH DATABASE {SNAP}")
            except sqlite3.Error:
                pass
    return ApplyResult(revision=revision, from_revision=local, kind="snapshot", counts=counts)


def _check_revision(local: int | None, revision: int, repair: bool) -> None:
    if local is None or revision > local:
        return
    if revision == local and repair:
        return
    raise RevisionMismatch(f"snapshot {revision} is not newer than the local revision {local}")


def install_snapshot_blob(conn: sqlite3.Connection, blob: bytes, *, sha256: str, revision: int,
                          app_version: str, tmp_dir: Path,
                          entity_counts: Mapping[str, int] | None = None, repair: bool = False,
                          now: Callable[[], datetime] = utc_now,
                          on_begin: Callable[[], None] | None = None) -> ApplyResult:
    """sha256 of the stored bytes -> bounded gunzip to a temp file -> validate -> apply -> delete the file"""
    require_sha256(blob, sha256, f"snapshot {revision}")
    return _install(conn, blob, sha256=sha256, revision=revision, app_version=app_version, tmp_dir=tmp_dir,
                    entity_counts=entity_counts, repair=repair, now=now, on_begin=on_begin)


BUNDLE_MANIFEST = "bundle.json"
_BUNDLE_FILE_RE = re.compile(r"^unison-([1-9][0-9]{0,14})\.sqlite\.gz$")


def read_bundle(directory: Path) -> tuple[int, Path]:
    """unison-N.sqlite.gz, never a path: {"revision": N, "file": "unison-N.sqlite.gz"}"""
    directory = Path(directory)
    try:
        doc = json.loads((directory / BUNDLE_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"bundle.json cannot be read: {exc}") from exc
    revision, name = (doc.get("revision"), doc.get("file")) if isinstance(doc, dict) else (None, None)
    match = _BUNDLE_FILE_RE.fullmatch(name) if isinstance(name, str) else None
    if (isinstance(revision, bool) or not isinstance(revision, int) or match is None
            or int(match.group(1)) != revision):
        raise SnapshotError("bundle.json must be {\"revision\": N, \"file\": \"unison-N.sqlite.gz\"}")
    return revision, directory / name


def install_bundled_snapshot(conn: sqlite3.Connection, path: Path, *, revision: int | None = None,
                             app_version: str, tmp_dir: Path, now: Callable[[], datetime] = utc_now,
                             on_begin: Callable[[], None] | None = None) -> ApplyResult:
    path = Path(path)
    if path.is_dir():
        found, path = read_bundle(path)
        if revision is not None and revision != found:
            raise SnapshotError(f"bundle holds revision {found}, not {revision}")
        revision = found
    elif revision is None:
        raise SnapshotError("a snapshot file needs its revision")
    blob = path.read_bytes()
    return _install(conn, blob, sha256=sha256_hex(blob), revision=revision, app_version=app_version,
                    tmp_dir=tmp_dir, entity_counts=None, repair=False, now=now, on_begin=on_begin)


def _install(conn: sqlite3.Connection, blob: bytes, *, sha256: str, revision: int, app_version: str,
             tmp_dir: Path, entity_counts: Mapping[str, int] | None, repair: bool,
             now: Callable[[], datetime], on_begin: Callable[[], None] | None = None) -> ApplyResult:
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=tmp_dir, prefix=f"snapshot-{revision}-", suffix=".sqlite")
    os.close(fd)
    target = Path(name)
    try:
        gunzip_to_file(blob, target, MAX_SNAPSHOT_BYTES)
        validate_snapshot(target, expected_layout(conn), entity_counts)
        return apply_snapshot(conn, target, revision=revision, package_sha256=sha256,
                              app_version=app_version, repair=repair, now=now, on_begin=on_begin)
    finally:
        for leftover in (target, Path(name + "-journal"), Path(name + "-wal"), Path(name + "-shm")):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass
