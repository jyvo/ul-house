"""local db"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ul_house.database import entities

SCHEMA_PATH: Path = entities.SCHEMA_PATH
MIGRATIONS_DIR: Path = Path(__file__).with_name("migrations")
APPLICATION_ID = 0x554C4855         # b"ULHU"
BUSY_TIMEOUT_MS = 5000

GAME_TABLES: tuple[str, ...] = entities.GAME_TABLES
LEDGER_TABLES: tuple[str, ...] = entities.LEDGER_TABLES
USER_TABLES: tuple[str, ...] = entities.section_tables("user")
SYNC_TABLES: tuple[str, ...] = entities.section_tables("sync")

_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class DatabaseError(Exception):
    """base class of database failures"""


class NotOurDatabase(DatabaseError):
    """the file has tables but no (or a foreign) application_id"""


class SchemaTooNew(DatabaseError):
    """user_version is above this app's schema_version: an older app never downgrades a file"""


class MigrationError(DatabaseError):
    """a gap or duplicate in the migrations, or a migration failed (and was rolled back)"""


@dataclass(frozen=True)
class Migration:
    version: int          # from file name NNNN_slug.sql
    name: str             # slug
    sql: str              # DDL/DML only: no BEGIN/COMMIT, no PRAGMA


def load_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """NNNN_slug.sql files sorted by version; versions must be 1..N with no gap or duplicate"""
    found: list[Migration] = []
    for path in sorted(Path(directory).iterdir()):
        match = _MIGRATION_RE.fullmatch(path.name)
        if match is None:
            continue
        found.append(Migration(int(match.group(1)), match.group(2), path.read_text(encoding="utf-8")))
    found.sort(key=lambda m: m.version)
    _check_contiguous(found)
    return tuple(found)


def _check_contiguous(migrations) -> None:
    versions = [m.version for m in migrations]
    if not versions:
        raise MigrationError("no migrations found")
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration versions must be 1..N without gaps or duplicates, got {versions}")


MIGRATIONS: tuple[Migration, ...] = load_migrations()
SCHEMA_VERSION: int = MIGRATIONS[-1].version


def quote_ident(name: str) -> str:
    """a SQL identifier; names come from our own schema, never from a package"""
    return '"' + name.replace('"', '""') + '"'


def connect(path: str | Path) -> sqlite3.Connection:
    """the only way client code opens the database file"""
    conn = sqlite3.connect(str(path), isolation_level=None, uri=True)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    conn.execute("PRAGMA trusted_schema = OFF")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def open_database(path: str | Path) -> sqlite3.Connection:
    """connect() and bring the file to the current schema"""
    conn = connect(path)
    try:
        ensure_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _pragma_int(conn: sqlite3.Connection, name: str) -> int:
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def ensure_schema(conn: sqlite3.Connection, *, schema_sql: str | None = None,
                  migrations: tuple[Migration, ...] = MIGRATIONS) -> int:
    """create a fresh file, migrate an old one, refuse a newer or foreign one; returns schema_version"""
    _check_contiguous(migrations)
    target = migrations[-1].version
    application_id = _pragma_int(conn, "application_id")
    version = _pragma_int(conn, "user_version")
    has_tables = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] > 0
    if application_id == 0 and version == 0 and not has_tables:
        if schema_sql is None:
            schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        create(conn, schema_sql, target)
        return target
    if application_id != APPLICATION_ID:
        raise NotOurDatabase(f"application_id {application_id:#x} is not ours ({APPLICATION_ID:#x})")
    if version > target:
        raise SchemaTooNew(f"database schema_version {version} is newer than this app's {target}")
    if version < target:
        migrate(conn, tuple(m for m in migrations if m.version > version))
    return target


def create(conn: sqlite3.Connection, schema_sql: str, version: int) -> None:
    """build the whole schema atomically; a failure leaves an empty file the next launch creates again"""
    conn.execute("PRAGMA journal_mode = WAL")
    script = (f"BEGIN IMMEDIATE;\n{schema_sql}\n;\n"
              f"PRAGMA application_id = {APPLICATION_ID};\nPRAGMA user_version = {int(version)};\nCOMMIT;")
    try:
        conn.executescript(script)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate(conn: sqlite3.Connection, migrations: tuple[Migration, ...]) -> int:
    version = _pragma_int(conn, "user_version")
    for m in migrations:
        if m.version <= version:
            continue
        if m.version != version + 1:
            raise MigrationError(f"migration {m.version} cannot follow schema_version {version}")
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{m.sql}\n;")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise MigrationError(f"{m.version:04d}_{m.name}: foreign key violations")
            conn.execute(f"PRAGMA user_version = {m.version}")
            conn.execute("COMMIT")
        except BaseException as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(exc, MigrationError):
                raise
            if isinstance(exc, Exception):
                raise MigrationError(f"{m.version:04d}_{m.name}: {exc}") from exc
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        version = m.version
    return version


def schema_version(conn: sqlite3.Connection) -> int:
    return _pragma_int(conn, "user_version")


def applied_revision(conn: sqlite3.Connection) -> int | None:
    """the dataset revision this file holds: max(sync_history.revision), None before the first sync"""
    return conn.execute("SELECT max(revision) FROM main.sync_history").fetchone()[0]


def game_tables(conn: sqlite3.Connection, schema: str = "main") -> tuple[str, ...]:
    tables = entities.GAME_TABLES + entities.LEDGER_TABLES
    members = set(tables)
    parents: dict[str, set[str]] = {}
    for table in tables:
        rows = conn.execute(f"PRAGMA {quote_ident(schema)}.foreign_key_list({quote_ident(table)})").fetchall()
        parents[table] = {row[2] for row in rows if row[2] in members and row[2] != table}
    ordered: list[str] = []
    placed: set[str] = set()
    while len(ordered) < len(tables):
        ready = [t for t in tables if t not in placed and parents[t] <= placed]
        if not ready:
            ready = [t for t in tables if t not in placed]
            ordered.extend(ready)
            break
        ordered.append(ready[0])
        placed.add(ready[0])
    return tuple(ordered)


def table_columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA {quote_ident(schema)}.table_info({quote_ident(table)})").fetchall()
    return tuple(row[1] for row in rows)


def table_decl(conn: sqlite3.Connection, table: str, schema: str = "main") -> tuple[tuple[str, str], ...]:
    """(column name, declared type) per column, for the P15 snapshot check"""
    rows = conn.execute(f"PRAGMA {quote_ident(schema)}.table_info({quote_ident(table)})").fetchall()
    return tuple((row[1], row[2]) for row in rows)


def primary_key(conn: sqlite3.Connection, table: str, schema: str = "main") -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA {quote_ident(schema)}.table_info({quote_ident(table)})").fetchall()
    return tuple(row[1] for row in sorted((r for r in rows if r[5] > 0), key=lambda r: r[5]))


def integer_key(conn: sqlite3.Connection, table: str, column: str) -> bool:
    for _cid, name, decl, *_ in conn.execute(f"PRAGMA main.table_info({quote_ident(table)})"):
        if name == column:
            return "INT" in (decl or "").upper()
    raise KeyError(f"{table}.{column}")


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM main.sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, items: Mapping[str, str | None]) -> None:
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        for key, value in items.items():
            conn.execute(
                "INSERT INTO main.sync_state (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
        if own:
            conn.execute("COMMIT")
    except BaseException:
        if own and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
