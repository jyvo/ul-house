"""seed + parsed records > DuckDB raw"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from ul_house import parse, rows
from ul_house.build.gate import utc_now
from ul_house.rows import Records

RAW_SCHEMA = "raw"
SEED_TABLES = ("page", "asset", "listing", "frontier", "evo_link", "crawl_run", "unlisted_release", "meta")
DROPPED_COLUMNS = {"page": ("html",), "asset": ("body",)}
DERIVED_COLUMNS = {"asset": ("body_bytes", "length(body)")}
SQLITE_TO_DUCKDB = {"INTEGER": "BIGINT", "TEXT": "VARCHAR", "REAL": "DOUBLE", "BLOB": "BLOB"}
RECORD_TO_DUCKDB = {"TEXT": "VARCHAR", "INTEGER": "BIGINT"}
SEED_SCHEMA_VERSION = "1"
TRANSFORM_RUN_JSON = "transform_run.json"

_INT_MIN, _INT_MAX = -(2**63), 2**63 - 1


class LoadError(Exception):
    """the seed or the records break the contract > the message names table/column/row"""


@dataclass(frozen=True)
class LoadReport:
    run_id: str
    started_at: str
    seed_sha256: str
    crawl_run_id: int
    parser_version: int
    counts: Mapping[str, int]
    parse_errors: int

    def to_mapping(self) -> dict:
        out = dataclasses.asdict(self)
        out["counts"] = dict(self.counts)
        return out


def new_run_id(started_at: str) -> str:
    """'t' + started_at (no separators) + '-' + 6 hex"""
    compact = started_at.replace("-", "").replace(":", "")
    return f"t{compact}-{os.urandom(3).hex()}"


def _connect_ro(seed: Path) -> sqlite3.Connection:
    seed = Path(seed)
    if not seed.is_file():
        raise LoadError(f"seed {seed} does not exist")
    wal = seed.with_name(seed.name + "-wal")
    uri = f"file:{seed}?mode=ro" if wal.exists() else f"file:{seed}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def read_pages(seed: Path) -> Iterator[tuple[str, str, bytes]]:
    """(item_id, html_sha256, html) for page.kind='detail' and html is not null, ordered by
    item_id; zlib-decompressed; sha256(html) != html_sha256 → LoadError (C1)."""
    con = _connect_ro(seed)
    try:
        cursor = con.execute("SELECT item_id, html_sha256, html FROM page "
                             "WHERE kind = 'detail' AND html IS NOT NULL ORDER BY item_id")
        for item_id, sha, blob in cursor:
            try:
                html = zlib.decompress(blob)
            except (zlib.error, TypeError) as e:
                raise LoadError(f"page {item_id}: html is not zlib data ({e})") from e
            if hashlib.sha256(html).hexdigest() != sha:
                raise LoadError(f"page {item_id}: sha256(html) != html_sha256 {sha!r} (C1)")
            yield item_id, sha, html
    finally:
        con.close()


def parse_seed(seed: Path, *, run_id: str) -> Records:
    return rows.rows([rows.parse_page(i, h, b) for i, h, b in read_pages(seed)],
                     run_id=run_id, parser_version=parse.PARSER_VERSION)


# write NDJSON + read_json
def _check_value(value, sql_type: str, nullable: bool, where: str, *, records: bool):
    if value is None:
        if not nullable:
            raise LoadError(f"{where}: NULL in a NOT NULL column")
        return None
    if isinstance(value, bool) and records:
        raise LoadError(f"{where}: bool is not a record type")
    if sql_type == "BIGINT":
        if isinstance(value, bool) or not isinstance(value, int):
            raise LoadError(f"{where}: expected an integer, got {type(value).__name__}")
        if not _INT_MIN <= value <= _INT_MAX:
            raise LoadError(f"{where}: integer {value} outside signed 64-bit")
        return value
    if sql_type == "VARCHAR":
        if not isinstance(value, str):
            raise LoadError(f"{where}: expected text, got {type(value).__name__}")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as e:
            raise LoadError(f"{where}: text is not valid UTF-8 ({e.reason})") from e
        return value
    if sql_type == "DOUBLE":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LoadError(f"{where}: expected a number, got {type(value).__name__}")
        return float(value)
    raise LoadError(f"{where}: unsupported column type {sql_type}")


def _create_table(con, table: str, columns: list[tuple[str, str, bool]]) -> None:
    body = ", ".join(f'"{name}" {sql_type}{"" if nullable else " NOT NULL"}'
                     for name, sql_type, nullable in columns)
    con.execute(f'CREATE TABLE {RAW_SCHEMA}."{table}" ({body})')


def _insert_rows(con, table: str, columns: list[tuple[str, str, bool]], values: Iterator[tuple], scratch: Path, *, records: bool) -> int:
    """validate and write NDJSON"""
    path = scratch / f"{table}.jsonl"
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in values:
            count += 1
            out = {}
            for (name, sql_type, nullable), value in zip(columns, row, strict=True):
                out[name] = _check_value(value, sql_type, nullable, f"{table}.{name} (row {count})", records=records)
            fh.write(json.dumps(out, ensure_ascii=False, allow_nan=False))
            fh.write("\n")
    if count:
        spec = "{" + ", ".join(f"'{name}': '{sql_type}'" for name, sql_type, _ in columns) + "}"
        names = ", ".join(f'"{name}"' for name, _, _ in columns)
        con.execute(f'INSERT INTO {RAW_SCHEMA}."{table}" ({names}) SELECT {names} FROM '
                    f"read_json(?, format = 'newline_delimited', columns = {spec}, "
                    "maximum_object_size = 268435456)", [str(path)])
    path.unlink()
    return count


def _seed_columns(src: sqlite3.Connection, table: str) -> tuple[list[tuple[str, str, bool]], list[str]]:
    """(DuckDB columns, sqlite select expressions) from PRAGMA table_xinfo"""
    columns: list[tuple[str, str, bool]] = []
    selects: list[str] = []
    dropped = DROPPED_COLUMNS.get(table, ())
    # noting pragma output cols
    for _cid, name, declared, notnull, _default, _pk, hidden in src.execute(f'PRAGMA table_xinfo("{table}")'):
        if hidden:
            continue
        if name in dropped:
            continue
        base = (declared or "").strip().upper().split("(")[0].strip()
        if base not in SQLITE_TO_DUCKDB:
            raise LoadError(f"{table}.{name}: unknown declared type {declared!r}")
        sql_type = SQLITE_TO_DUCKDB[base]
        if sql_type == "BLOB":
            raise LoadError(f"{table}.{name}: BLOB columns other than {DROPPED_COLUMNS} cannot be loaded")
        columns.append((name, sql_type, not notnull))
        selects.append(f'"{name}"')
    if table in DERIVED_COLUMNS:
        name, expr = DERIVED_COLUMNS[table]
        columns.append((name, "BIGINT", True))
        selects.append(expr)
    return columns, selects


def load_seed_tables(con, seed: Path, *, scratch: Path | None = None) -> dict[str, int]:
    """copy SEED_TABLES into raw (bytes dropped, body_bytes derived) -> {table: rows}"""
    src = _connect_ro(seed)
    own_scratch = scratch is None
    scratch = Path(scratch) if scratch is not None else Path(tempfile.mkdtemp(prefix="ul_load_"))
    scratch.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    try:
        for table in SEED_TABLES:
            columns, selects = _seed_columns(src, table)
            _create_table(con, table, columns)
            cursor = src.execute(f'SELECT {", ".join(selects)} FROM "{table}"')
            counts[table] = _insert_rows(con, table, columns, iter(cursor), scratch, records=False)
    finally:
        src.close()
        if own_scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    return counts


def record_columns(table: str) -> list[tuple[str, str, bool]]:
    """(column, DuckDB type, nullable) of a Records table"""
    return [(name, RECORD_TO_DUCKDB[sql_type], nullable)
            for name, sql_type, nullable in rows.columns(rows.RECORD_TYPES[table])]


def load_records(con, records: Records, *, scratch: Path | None = None) -> dict[str, int]:
    """one raw table per records list, parse_errors -> raw.parse_error"""
    own_scratch = scratch is None
    scratch = Path(scratch) if scratch is not None else Path(tempfile.mkdtemp(prefix="ul_load_"))
    scratch.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    try:
        for table, items in records.tables().items():
            columns = record_columns(table)
            _create_table(con, table, columns)
            names = [name for name, _, _ in columns]
            values = (tuple(getattr(item, name) for name in names) for item in items)
            counts[table] = _insert_rows(con, table, columns, values, scratch, records=True)
    finally:
        if own_scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    return counts


# entry
def _seed_meta(seed: Path) -> dict[str, str]:
    con = _connect_ro(seed)
    try:
        tables = {t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = [t for t in SEED_TABLES if t not in tables]
        if missing:
            raise LoadError(f"seed {seed} lacks table(s) {missing}")
        return dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_seed(seed: Path, work_dir: Path, *, run_id: str | None = None,
              started_at: str | None = None, records: Records | None = None,
              allow_partial: bool = False, clock: Callable[[], str] = utc_now) -> LoadReport:
    """seed.sqlite -> work/mart.duckdb (schema raw) + work/transform_run.json"""
    import duckdb               # build group only, considering package lazy load for later
    seed = Path(seed)
    work_dir = Path(work_dir)
    meta = _seed_meta(seed)
    version = meta.get("seed.schema_version")
    if version != SEED_SCHEMA_VERSION:
        raise LoadError(f"seed schema_version {version!r} != {SEED_SCHEMA_VERSION!r}")
    last = meta.get("crawl.last_complete_run")
    if last is None or not str(last).strip().isdigit():
        raise LoadError(f"seed has no complete crawl run (crawl.last_complete_run = {last!r})")
    active = meta.get("crawl.active_run")
    if active not in (None, "") and not allow_partial:
        raise LoadError(f"seed has an active crawl run ({active}); the data workflow never transforms a partial crawl (allow_partial=True is for development)")

    started_at = started_at or clock()
    run_id = run_id or new_run_id(started_at)
    seed_sha256 = _sha256_file(seed)
    if records is None:
        records = parse_seed(seed, run_id=run_id)

    work_dir.mkdir(parents=True, exist_ok=True)
    mart = work_dir / "mart.duckdb"
    for stale in (mart, mart.with_name(mart.name + ".wal")):
        stale.unlink(missing_ok=True)
    scratch = work_dir / "load"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    con = duckdb.connect(str(mart))
    try:
        con.execute(f"CREATE SCHEMA {RAW_SCHEMA}")
        counts = load_seed_tables(con, seed, scratch=scratch)
        counts.update(load_records(con, records, scratch=scratch))
        con.execute("CHECKPOINT")
    except Exception:
        con.close()
        mart.unlink(missing_ok=True)
        raise
    else:
        con.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    report = LoadReport(run_id=run_id, started_at=started_at, seed_sha256=seed_sha256,
                        crawl_run_id=int(last), parser_version=records.parser_version,
                        counts=counts, parse_errors=len(records.parse_errors))
    (work_dir / TRANSFORM_RUN_JSON).write_text(
        json.dumps(report.to_mapping(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
