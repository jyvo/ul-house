from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import uuid
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

import duckdb

from ul_house.build.store import (
    CHUNK,
    IntegrityError,
    Layout,
    ObjectMissing,
    ObjectStore,
    sha256_bytes,
    sha256_file,
)
from ul_house.database import entities as E
from ul_house.sync.protocol import API_SCHEMA_VERSION
from ul_house.sync.verify import canonical_bytes

PACKAGE_WINDOW = 30                  # package window
SNAPSHOT_ONLY_RATIO = 0.5            # package-to-snapshot size ratio
OPERATIONS = ("INSERT", "UPDATE", "RETIRE", "ALIAS", "DELETE")      # operation names
OP_ENTITY_ORDER = ("element", "icon", "item", "skill_effect", "proc_family", "proc",
                   "passive_skill", "weapon_ability", "equipment", "uid_retired", "uid_alias")
SNAPSHOT_ONLY_REASONS = ("package_size", "dataset_schema_version", "catalog_version", "forced",
                         "first_release", "no_base")                  # manifest enum plus no_base
SUBTYPE_TABLES = ("weapon", "defensive_gear", "monster")               # 1:1 subtype objects of equipment
LEDGER_ENTITIES = ("uid_retired", "uid_alias")
BUNDLE_VERSION = 1
DEFAULT_CONTRACT_DIR = Path(__file__).resolve().parents[3] / "api" / "contract" / "schemas"
CONTENT_TYPES = (
    (".json.gz", "application/gzip"), (".sqlite.gz", "application/gzip"), (".json", "application/json"),
    (".png", "image/png"), (".zip", "application/zip"), (".sqlite", "application/vnd.sqlite3"),
    (".duckdb", "application/octet-stream"),
)
_ZERO_COMMIT = "0" * 40
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class ReleaseError(Exception):
    pass


class ContractError(ReleaseError):
    """Contract or mart shape mismatch."""


class ReplayError(ReleaseError):
    """Replay mismatch."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text(dt: datetime) -> str:
    """ISO-8601 UTC seconds with Z"""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def content_type_of(path: str) -> str:
    for suffix, ctype in CONTENT_TYPES:
        if path.endswith(suffix):
            return ctype
    return "application/octet-stream"


def _commit(value: Any) -> str:
    """40-hex or zero commit"""
    return value if isinstance(value, str) and _COMMIT_RE.match(value) else _ZERO_COMMIT


def gzip_bytes(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", compresslevel=9, mtime=0, fileobj=buf) as gz:
        gz.write(data)
    return buf.getvalue()


def gzip_file(src: Path, dest: Path) -> tuple[str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fh, open(dest, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=9, mtime=0, fileobj=raw) as gz:
            shutil.copyfileobj(fh, gz, CHUNK)
    return sha256_file(dest), dest.stat().st_size


def gunzip_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(src, "rb") as fh, open(dest, "wb") as out:
        shutil.copyfileobj(fh, out, CHUNK)


def write_json_gz(obj: Any, path: Path) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = gzip_bytes(canonical_bytes(obj))
    path.write_bytes(data)
    return sha256_bytes(data), len(data)


def write_json(obj: Any, path: Path) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(obj)
    path.write_bytes(data)
    return sha256_bytes(data), len(data)


def read_json_gz(path: Path) -> Any:
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read())


def duck_sql(predicate: str) -> str:
    """convert predicates to DuckDB parameters"""
    return re.sub(r"(?<!:):(key_text|key)\b", r"$\1", predicate)


def _has_table(conn: duckdb.DuckDBPyConnection, name: str, schema: str = "main") -> bool:
    row = conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
        [schema, name]).fetchone()
    return bool(row and row[0])


def _columns(conn: duckdb.DuckDBPyConnection, table: str, schema: str = "main") -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = ? AND table_name = ? "
        "ORDER BY ordinal_position", [schema, table]).fetchall()]


def _dicts(conn: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, list(params))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


@contextmanager
def _duck(source: Path | str | duckdb.DuckDBPyConnection):
    """read-only DuckDB connection"""
    if isinstance(source, duckdb.DuckDBPyConnection):
        yield source
        return
    conn = duckdb.connect(str(source), read_only=True)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _sqlite_ro(path: Path):
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


# types
def _git(repo: Path, *args: str) -> str | None:
    """run read-only git"""
    try:
        proc = subprocess.Popen(["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        out, _ = proc.communicate(timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.strip() if proc.returncode == 0 else None


def _version(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return "absent"


@dataclass(frozen=True)
class BuildInfo:
    build_commit: str                   # $GITHUB_SHA (40-HEX) | `git rev-parse HEAD`
    dirty: bool                         # git status
    run_id: str                         # $GITHUB_RUN_ID | "local-<uuid4 hex[:12]>"
    runtime: Mapping[str, str]

    @classmethod
    def collect(cls, repo_root: Path, environ: Mapping[str, str] | None = None) -> BuildInfo:
        env = os.environ if environ is None else environ
        sha = env.get("GITHUB_SHA")
        if sha and _COMMIT_RE.match(sha):
            commit, dirty = sha, False
        else:
            head = _git(Path(repo_root), "rev-parse", "HEAD")
            commit = head if head and _COMMIT_RE.match(head) else _ZERO_COMMIT
            status = _git(Path(repo_root), "status", "--porcelain")
            dirty = True if status is None else bool(status)
        run = env.get("GITHUB_RUN_ID")
        run_id = run if run and _RUN_ID_RE.match(run) else f"local-{uuid.uuid4().hex[:12]}"
        runtime = {
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "dbt_core": _version("dbt-core"),
            "dbt_duckdb": _version("dbt-duckdb"),
            "sqlite": sqlite3.sqlite_version,
            "cryptography": _version("cryptography"),
        }
        return cls(commit, dirty, run_id, runtime)


@dataclass(frozen=True)
class GateSummary:
    level: Literal["pass", "warning", "blocker", "error"]
    policy_version: str
    policy_commit: str
    findings: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any]) -> GateSummary:
        level = m.get("level")
        if level not in ("pass", "warning", "blocker", "error"):
            raise ReleaseError(f"gate.json: unknown level {level!r}")
        return cls(level, str(m.get("policy_version", "")), str(m.get("policy_commit", "")),
                   tuple(m.get("findings") or ()))

    @classmethod
    def load(cls, path: Path) -> GateSummary:
        try:
            return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))
        except FileNotFoundError:
            raise ReleaseError(f"{path}: no gate decision (run the gate stage first)") from None

    def warnings(self) -> list[dict[str, Any]]:
        """gate findings for manifest"""
        out = []
        for f in self.findings:
            if f.get("level") not in ("warning", "blocker"):
                continue
            pct = f.get("pct")
            if pct is not None:
                pct = min(100.0, max(0.0, float(pct)))
            out.append({"check": f["check"], "scope": f["scope"], "level": f["level"],
                        "count": int(f.get("count", 0)), "pct": pct,
                        "message": str(f.get("message", ""))[:500]})
        return out


@dataclass(frozen=True)
class Change:
    revision: int
    entity_type: str
    entity_key: str
    operation: str
    row_hash: str | None = None


@dataclass(frozen=True)
class ReleaseInputs:
    work_dir: Path
    revision: int
    store: ObjectStore                                      # read-only
    build: BuildInfo
    clock: Callable[[], datetime] = utc_now
    force_snapshot: bool = False
    contract_dir: Path | None = DEFAULT_CONTRACT_DIR        # None -> validation skipped (tests only)
    schema_text: str | None = None                          # None -> entities.SCHEMA_PATH
    dataset_schema_version: int = E.DATASET_SCHEMA_VERSION
    api_schema_version: int = API_SCHEMA_VERSION
    window: int = PACKAGE_WINDOW
    snapshot_only_ratio: float = SNAPSHOT_ONLY_RATIO


@dataclass(frozen=True)
class ReleaseDecision:
    action: Literal["publish", "hold", "heartbeat"]
    revision: int
    gate_level: str
    bundle_dir: Path | None
    reason: str
    carried_forward: tuple[str, ...] = ()    # parse-failed uids carried forward

    def to_mapping(self, work_dir: Path | None = None) -> dict[str, Any]:
        bundle: str | None = None
        if self.bundle_dir is not None:
            bundle = str(self.bundle_dir)
            if work_dir is not None:
                try:
                    bundle = Path(self.bundle_dir).relative_to(work_dir).as_posix()
                except ValueError:
                    pass
        out: dict[str, Any] = {"action": self.action, "revision": self.revision, "gate_level": self.gate_level,
                               "bundle": bundle, "reason": self.reason}
        if self.carried_forward:
            out["carried_forward"] = list(self.carried_forward)
        return out

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any], work_dir: Path | None = None) -> ReleaseDecision:
        bundle = m.get("bundle")
        path = None
        if bundle:
            path = Path(bundle)
            if not path.is_absolute() and work_dir is not None:
                path = Path(work_dir) / path
        return cls(m["action"], int(m["revision"]), m["gate_level"], path, m.get("reason", ""),
                   tuple(m.get("carried_forward", ())))

    @classmethod
    def load(cls, work_dir: Path) -> ReleaseDecision:
        path = Path(work_dir) / "release" / "decision.json"
        try:
            return cls.from_mapping(json.loads(path.read_text(encoding="utf-8")), Path(work_dir))
        except FileNotFoundError:
            raise ReleaseError(f"{path}: no release decision (run the release stage first)") from None

\
# shipped tables, mart shape
def shipped_tables(schema_text: str | None = None) -> tuple[str, ...]:
    """game then ledger tables, file order"""
    sections = E.schema_sections(schema_text)
    pattern = E._CREATE_TABLE_RE
    return tuple(pattern.findall(sections["game"])) + tuple(pattern.findall(sections["ledger"]))


def check_entities(mart: Path | duckdb.DuckDBPyConnection, shipped: Sequence[str]) -> None:
    """check shipped tables are entity roots"""
    covered = {e.root_table for e in E.ENTITIES} | {c.table for e in E.ENTITIES for c in e.children}
    missing = [t for t in shipped if t not in covered]
    if missing:
        raise ContractError(f"tables not covered by any aggregate: {', '.join(missing)}")
    with _duck(mart) as conn:
        for table in ("evolution_edge", "evolution_material"):
            if table not in shipped or not _has_table(conn, table):
                continue
            bad = conn.execute(
                f"SELECT count(*) FROM {table} t WHERE NOT EXISTS "
                f"(SELECT 1 FROM equipment e WHERE e.uid = t.from_uid OR e.uid = t.to_uid)").fetchone()[0]
            if bad:
                raise ContractError(f"{table}: {bad} row(s) with no shipped endpoint (P11)")


_DUCK_TO_SQLITE = {
    "VARCHAR": "TEXT", "TEXT": "TEXT", "BIGINT": "INTEGER", "INTEGER": "INTEGER", "SMALLINT": "INTEGER",
    "TINYINT": "INTEGER", "HUGEINT": "INTEGER", "UBIGINT": "INTEGER", "UINTEGER": "INTEGER",
    "USMALLINT": "INTEGER", "UTINYINT": "INTEGER", "BOOLEAN": "INTEGER", "DOUBLE": "REAL", "REAL": "REAL",
    "FLOAT": "REAL",
}


def _check_mart_shape(conn: duckdb.DuckDBPyConnection, lite: sqlite3.Connection, tables: Sequence[str]) -> None:
    present = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()}
    present.discard(PARSE_FAILURE_TABLE)            # not a shipped table
    extra, absent = sorted(present - set(tables)), sorted(set(tables) - present)
    if extra or absent:
        raise ContractError(f"mart.duckdb tables differ from schema.sql: extra {extra}, missing {absent}")
    for table in tables:
        duck = {r[0]: str(r[1]).upper() for r in conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'main' "
            "AND table_name = ?", [table]).fetchall()}
        lite_cols = {r[1]: (r[2] or "").upper() for r in lite.execute(f'PRAGMA table_info("{table}")')}
        if set(duck) != set(lite_cols):
            raise ContractError(f"{table}: mart columns {sorted(duck)} != schema columns {sorted(lite_cols)}")
        for col, dtype in duck.items():
            mapped = _DUCK_TO_SQLITE.get(dtype)
            if mapped is None:
                raise ContractError(f"{table}.{col}: mart type {dtype} has no snapshot mapping "
                                    "(timestamps are text, P10)")
            if mapped != lite_cols[col]:
                raise ContractError(f"{table}.{col}: mart type {dtype} is not schema type {lite_cols[col]}")


@dataclass(frozen=True)
class SnapshotInfo:
    sqlite_path: Path
    gz_path: Path
    sqlite_sha256: str
    sqlite_bytes: int
    sha256: str                         # the .gz as stored
    bytes: int
    counts: Mapping[str, int]
    table_hashes: Mapping[str, str]
    carried: tuple[str, ...] = ()               # uids carried from previous snapshot
    carry_warnings: tuple[str, ...] = ()        # listed uids not carried
    carried_shared: Mapping[str, int] = field(default_factory=dict)   # shared rows copied per table


def table_hash(conn: sqlite3.Connection, table: str) -> str:
    """order-independent table digest"""
    cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{table}")')]
    if not cols:
        raise ContractError(f"{table}: no such table")
    h = hashlib.sha256(canonical_bytes({"table": table, "columns": cols}))
    quoted = ", ".join(f'"{c}"' for c in cols)
    lines = []
    for row in conn.execute(f'SELECT {quoted} FROM "{table}"'):
        if any(isinstance(v, (bytes, bytearray, memoryview)) for v in row):
            raise ContractError(f"{table}: BLOB value (game tables hold none, §13.4)")
        lines.append(json.dumps(list(row), separators=(",", ":"), ensure_ascii=True, allow_nan=False))
    for line in sorted(lines):
        h.update(line.encode("ascii") + b"\n")
    return h.hexdigest()


def table_hashes(sqlite_path: Path, tables: Sequence[str]) -> dict[str, str]:
    with _sqlite_ro(sqlite_path) as conn:
        return {t: table_hash(conn, t) for t in tables}


PARSE_FAILURE_TABLE = "shipped_parse_failures"
PARSE_FAILURE_SCHEMAS = ("build", "main")       # exported shipped parse failures


def read_parse_failures(mart: Path | duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """read parse-failure rows from mart"""
    with _duck(mart) as conn:
        for schema in PARSE_FAILURE_SCHEMAS:
            if _has_table(conn, PARSE_FAILURE_TABLE, schema):
                have = set(_columns(conn, PARSE_FAILURE_TABLE, schema))
                if "uid" not in have:
                    raise ContractError(f"{schema}.{PARSE_FAILURE_TABLE}: column uid is missing")
                select = ", ".join(c if c in have else f"NULL AS {c}"
                                   for c in ("uid", "source_hash", "parser_version", "error"))
                return _dicts(conn, f'SELECT {select} FROM {schema}."{PARSE_FAILURE_TABLE}" ORDER BY uid')
    return []


# foreign keys absent from schema.sql
LOGICAL_REFS: dict[tuple[str, str], tuple[str, str]] = {
    ("equipment", "icon_sha"): ("icon", "sha256"),
    ("evolution_material", "ref_uid"): ("item", "uid"),
}


def shared_roots() -> frozenset[str]:
    """shared entity root tables"""
    return frozenset(e.root_table for e in E.ENTITIES if e.entity_type != "equipment" and e.deletable)


def reference_graph(conn: sqlite3.Connection, tables: Iterable[str]) -> dict[str, list[tuple[str, str, str]]]:
    """foreign keys per table"""
    roots = shared_roots()
    graph: dict[str, list[tuple[str, str, str]]] = {}
    for table in tables:
        refs = [(fk[3], fk[2], fk[4]) for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")')
                if fk[2] in roots]
        refs += [(col, parent, pcol) for (t, col), (parent, pcol) in LOGICAL_REFS.items() if t == table]
        if refs:
            graph[table] = sorted(refs)
    return graph


def _carry_shared(lite: sqlite3.Connection, old: sqlite3.Connection, seeds: Sequence[tuple[str, list[str], list[tuple]]]) -> dict[str, int]:
    """copy referenced rows from old snapshot"""
    tables = [r[0] for r in lite.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    graph = reference_graph(lite, tables)
    by_root = {e.root_table: e for e in E.ENTITIES}
    counts: dict[str, int] = defaultdict(int)

    def insert(table: str, where: str, params: tuple) -> tuple[list[str], list[tuple]]:
        have = {r[1] for r in old.execute(f'PRAGMA table_info("{table}")')}
        cols = [r[1] for r in lite.execute(f'PRAGMA table_info("{table}")') if r[1] in have]
        quoted = ", ".join(f'"{c}"' for c in cols)
        rows = old.execute(f'SELECT {quoted} FROM "{table}" WHERE {where}', params).fetchall()
        if rows:
            before = lite.total_changes
            lite.executemany(f'INSERT OR IGNORE INTO "{table}" ({quoted}) VALUES ({", ".join("?" * len(cols))})', rows)
            counts[table] += lite.total_changes - before
        return cols, rows

    work = list(seeds)
    while work:
        table, cols, rows = work.pop()
        for column, parent, pcolumn in graph.get(table, ()):
            at = cols.index(column)
            for row in rows:
                value = row[at]
                if value is None:
                    continue
                if (table, column) == ("evolution_material", "ref_uid") and row[cols.index("material_kind")] != "item":
                    continue
                if lite.execute(f'SELECT 1 FROM "{parent}" WHERE "{pcolumn}" = ?', (value,)).fetchone():
                    continue
                pcols, prows = insert(parent, f'"{pcolumn}" = ?', (value,))
                if not prows:
                    continue                                    # not in the previous snapshot either
                work.append((parent, pcols, prows))
                ent = by_root[parent]
                key = prows[0][pcols.index(ent.key_columns[0])]
                for child in ent.children:                      # its conditions, scaling, effect links
                    cwhere, cparams = E.child_where(ent, child, key)
                    ccols, crows = insert(child.table, cwhere, cparams)
                    if crows:
                        work.append((child.table, ccols, crows))
    return {t: n for t, n in sorted(counts.items()) if n}


def _carry_forward(lite: sqlite3.Connection, prev_snapshot: Path, uids: Sequence[str]) -> tuple[list[str], list[str], dict[str, int]]:
    """copy whole aggregates from old snapshot"""
    ent = E.entity("equipment")
    carried: list[str] = []
    warnings: list[str] = []
    seeds: list[tuple[str, list[str], list[tuple]]] = []
    with _sqlite_ro(prev_snapshot) as old:
        def copy(table: str, where: str, params: tuple, *, ignore: bool) -> None:
            have = {r[1] for r in old.execute(f'PRAGMA table_info("{table}")')}
            cols = [r[1] for r in lite.execute(f'PRAGMA table_info("{table}")') if r[1] in have]
            quoted = ", ".join(f'"{c}"' for c in cols)
            rows = old.execute(f'SELECT {quoted} FROM "{table}" WHERE {where}', params).fetchall()
            verb = "INSERT OR IGNORE" if ignore else "INSERT"
            lite.executemany(f'{verb} INTO "{table}" ({quoted}) VALUES ({", ".join("?" * len(cols))})', rows)
            seeds.append((table, cols, rows))

        for uid in uids:
            where, params = E.root_where(ent, uid)
            if not old.execute(f'SELECT 1 FROM "{ent.root_table}" WHERE {where}', params).fetchone():
                warnings.append(f"{uid}: listed in shipped_parse_failures but not in the previous snapshot; ignored")
                continue
            if lite.execute(f'SELECT 1 FROM "{ent.root_table}" WHERE {where}', params).fetchone():
                warnings.append(f"{uid}: listed in shipped_parse_failures but the mart has its row; not carried")
                continue
            copy(ent.root_table, where, params, ignore=False)
            for child in reversed(ent.children):            # root first, children reversed
                cwhere, cparams = E.child_where(ent, child, uid)
                copy(child.table, cwhere, cparams, ignore=True)
            carried.append(uid)
        shared = _carry_shared(lite, old, seeds)
    return carried, warnings, shared


def build_snapshot(mart: Path, out_dir: Path, *, revision: int, dataset_schema_version: int,
                   schema_text: str | None = None, prev_snapshot: Path | None = None) -> SnapshotInfo:
    """build snapshot of mart at revision"""
    sections = E.schema_sections(schema_text)
    tables = shipped_tables(schema_text)
    out_dir = Path(out_dir)
    tmp = out_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    sqlite_path = tmp / f"unison-{revision}.sqlite"
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(sqlite_path) + suffix).unlink(missing_ok=True)

    lite = sqlite3.connect(sqlite_path, isolation_level=None)
    stage = "create"
    carried: list[str] = []
    carry_warnings: list[str] = []
    carried_shared: dict[str, int] = {}
    try:
        lite.execute("PRAGMA journal_mode = DELETE")
        lite.execute("PRAGMA page_size = 4096")
        lite.execute("PRAGMA foreign_keys = ON")
        lite.executescript(sections["game"])
        lite.executescript(sections["ledger"])
        bad = lite.execute("SELECT type, name FROM sqlite_master WHERE type NOT IN ('table', 'index')").fetchall()
        if bad:
            raise ContractError(f"snapshot DDL contains views or triggers: {bad} (P15)")
        with _duck(mart) as conn:
            _check_mart_shape(conn, lite, tables)
            lite.execute("BEGIN")
            lite.execute("PRAGMA defer_foreign_keys = ON")
            for table in tables:
                stage = f"insert into {table}"
                info = list(lite.execute(f'PRAGMA table_info("{table}")'))
                cols = [r[1] for r in info]
                pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
                quoted = ", ".join(f'"{c}"' for c in cols)
                order = ", ".join(f'"{c}"' for c in pk) if pk else "ALL"
                cur = conn.execute(f'SELECT {quoted} FROM main."{table}" ORDER BY {order}')
                insert = f'INSERT INTO "{table}" ({quoted}) VALUES ({", ".join("?" * len(cols))})'
                while batch := cur.fetchmany(10000):
                    lite.executemany(insert, [tuple(int(v) if isinstance(v, bool) else v for v in row)
                                              for row in batch])
            failures = read_parse_failures(conn)
            if failures and prev_snapshot is None:
                carry_warnings.append(f"{len(failures)} shipped_parse_failures row(s) ignored: no previous snapshot")
            elif failures:
                stage = "carry forward"
                carried, carry_warnings, carried_shared = _carry_forward(
                    lite, prev_snapshot, [f["uid"] for f in failures])
            stage = "commit"
            try:
                lite.execute("COMMIT")                  # deferred foreign keys are checked here
            except sqlite3.IntegrityError as exc:
                lite.execute("ROLLBACK")
                raise ReleaseError(f"snapshot foreign_key_check: {exc}") from None
        stage = "check"
        violations = lite.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ReleaseError(f"snapshot foreign_key_check: {violations[:5]}")
        check = lite.execute("PRAGMA integrity_check").fetchall()
        if check != [("ok",)]:
            raise ReleaseError(f"snapshot integrity_check: {check[:5]}")
        lite.execute(f"PRAGMA user_version = {int(dataset_schema_version)}")
        lite.execute("VACUUM")
        counts = {t: lite.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    except sqlite3.DatabaseError as exc:                # constraint and DDL errors
        raise ReleaseError(f"snapshot {stage}: {type(exc).__name__}: {exc}") from None
    finally:
        lite.close()
    hashes = table_hashes(sqlite_path, tables)
    gz_path = out_dir / "pub" / Layout.rel_snapshot(revision)
    sha, size = gzip_file(sqlite_path, gz_path)
    return SnapshotInfo(sqlite_path, gz_path, sha256_file(sqlite_path), sqlite_path.stat().st_size,
                        sha, size, counts, hashes, tuple(carried), tuple(carry_warnings), carried_shared)


# changes, net operations
def read_changes(history: Path | duckdb.DuckDBPyConnection, after: int, upto: int) -> list[Change]:
    """change log rows in window"""
    with _duck(history) as conn:
        if not _has_table(conn, "dataset_changes"):
            raise ReleaseError("history: table dataset_changes is missing")
        rows = conn.execute(
            "SELECT revision, entity_type, entity_key, operation, row_hash FROM dataset_changes "
            "WHERE revision > ? AND revision <= ? ORDER BY revision, entity_type, entity_key",
            [after, upto]).fetchall()
    return [Change(int(r[0]), r[1], r[2], r[3], r[4]) for r in rows]


def alias_end(mart: Path | duckdb.DuckDBPyConnection) -> dict[str, str]:
    """resolve alias chains to their end"""
    with _duck(mart) as conn:
        direct = {r[0]: r[1] for r in conn.execute("SELECT old_uid, new_uid FROM uid_alias").fetchall()}
    ends: dict[str, str] = {}
    for old, first in direct.items():
        seen = [old]
        cur = first
        while cur in direct:
            if cur in seen:
                raise ReleaseError(f"uid_alias cycle: {' -> '.join(seen + [cur])}")
            seen.append(cur)
            cur = direct[cur]
        if cur == old:
            raise ReleaseError(f"uid_alias {old} points at itself")
        ends[old] = cur
    return ends


def _entity(entity_type: str) -> E.Entity:
    try:
        return E.entity(entity_type)
    except KeyError:
        raise ReleaseError(f"unknown entity type {entity_type!r}") from None


def _native_key(ent: E.Entity, text: str) -> int | str:
    try:
        return E.key_from_text(ent, text)
    except (ValueError, TypeError) as exc:
        raise ReleaseError(f"{ent.entity_type}: bad key {text!r}: {exc}") from None


def aggregate_data(conn: duckdb.DuckDBPyConnection, entity: E.Entity, key: int | str) -> dict[str, Any]:
    """payload of an UPSERT"""
    where, params = E.root_where(entity, key)
    rows = _dicts(conn, f'SELECT * FROM main."{entity.root_table}" WHERE {where}', params)
    if not rows:
        raise ReleaseError(f"{entity.entity_type} {key!r}: no root row in mart at the target revision")
    data: dict[str, Any] = {entity.root_table: rows[0]}
    tables: dict[str, list[dict[str, Any]]] = {}
    for child in entity.children:
        sql, cparams = E.child_where(entity, child, key)
        got = _dicts(conn, f'SELECT * FROM main."{child.table}" WHERE {sql} ORDER BY ALL', cparams)
        tables.setdefault(child.table, []).extend(got)
    for table, got in tables.items():
        if table in SUBTYPE_TABLES:
            if got:
                data[table] = got[0]
        else:
            data[table] = got
    return data


def net_ops(changes: Sequence[Change], *, mart: Path | duckdb.DuckDBPyConnection,
            alias_end: Mapping[str, str]) -> list[dict[str, Any]]:
    """collapse window changes to net operations"""
    groups: dict[tuple[str, str], list[Change]] = defaultdict(list)
    for c in changes:
        if c.operation not in OPERATIONS:
            raise ReleaseError(f"unknown operation {c.operation!r} on {c.entity_type} {c.entity_key}")
        groups[(c.entity_type, c.entity_key)].append(c)

    upserts: list[tuple[int, int | str, dict]] = []     # (order, native key, op)
    retires: list[tuple[int | str, dict]] = []
    aliases: list[tuple[int | str, dict]] = []
    deletes: list[tuple[int, int | str, dict]] = []

    with _duck(mart) as conn:
        for (etype, ekey), cs in sorted(groups.items()):
            ent = _entity(etype)
            native = _native_key(ent, ekey)
            cs = sorted(cs, key=lambda c: c.revision)
            ops = [c.operation for c in cs]
            state = [c for c in cs if c.operation != "ALIAS"]
            alias = [c for c in cs if c.operation == "ALIAS"]
            order = OP_ENTITY_ORDER.index(etype)

            if etype in LEDGER_ENTITIES:
                if any(o != "INSERT" for o in ops):
                    raise ReleaseError(f"{etype} {ekey}: ledger rows are append-only, got {ops}")
                data = aggregate_data(conn, ent, native)
                upserts.append((order, native, {"op": "UPSERT", "entity": etype, "key": ekey,
                                                "rev": state[-1].revision, "data": data}))
                continue

            if state:
                first, last = state[0].operation, state[-1].operation
                if last == "DELETE":
                    if first != "INSERT":
                        if not ent.deletable:
                            raise ReleaseError(f"{etype} {ekey}: DELETE on a type that is never deleted")
                        deletes.append((order, native, {"op": "DELETE", "entity": etype, "key": ekey,
                                                        "rev": state[-1].revision}))
                elif any(c.operation in ("INSERT", "UPDATE") for c in state):
                    upserts.append((order, native, {"op": "UPSERT", "entity": etype, "key": ekey,
                                                    "rev": state[-1].revision,
                                                    "data": aggregate_data(conn, ent, native)}))
                else:                                          # only RETIRE
                    if not ent.retirable:
                        raise ReleaseError(f"{etype} {ekey}: RETIRE on a type that is never retired")
                    row = conn.execute("SELECT reason FROM uid_retired WHERE uid = ?", [ekey]).fetchone()
                    if row is None:
                        raise ReleaseError(f"equipment {ekey}: RETIRE without a uid_retired row")
                    retires.append((native, {"op": "RETIRE", "entity": etype, "key": ekey,
                                             "rev": state[-1].revision, "reason": row[0]}))
            if alias:
                if not ent.aliasable:
                    raise ReleaseError(f"{etype} {ekey}: ALIAS on a type that is never aliased")
                end = alias_end.get(ekey)
                if end is None:
                    raise ReleaseError(f"{etype} {ekey}: ALIAS without a uid_alias row")
                row = conn.execute("SELECT confirmed_commit FROM uid_alias WHERE old_uid = ?", [ekey]).fetchone()
                if row is None:
                    raise ReleaseError(f"{etype} {ekey}: ALIAS without a uid_alias row")
                aliases.append((native, {"op": "ALIAS", "entity": etype, "key": ekey, "to": end,
                                         "rev": alias[-1].revision, "confirmed_commit": row[0]}))

    out = [op for _, _, op in sorted(upserts, key=lambda t: (t[0], t[1]))]
    out += [op for _, op in sorted(retires, key=lambda t: t[0])]
    out += [op for _, op in sorted(aliases, key=lambda t: t[0])]
    out += [op for _, _, op in sorted(deletes, key=lambda t: (-t[0], t[1]))]
    return out


def build_package(*, from_revision: int, to_revision: int, dataset_schema_version: int,
                  ops: Sequence[Mapping[str, Any]], icons: Mapping[str, Any]) -> dict[str, Any]:
    """package shape without ledger block"""
    return {"from_revision": from_revision, "to_revision": to_revision,
            "dataset_schema_version": dataset_schema_version, "ops": list(ops), "icons": dict(icons)}


# reference applier and replay
def _insert_rows(conn: sqlite3.Connection, table: str, rows: Iterable[Mapping[str, Any]]) -> None:
    for row in rows:
        cols = list(row)
        conn.execute(f'INSERT INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) '
                     f'VALUES ({", ".join("?" * len(cols))})', [row[c] for c in cols])


def _upsert_root(conn: sqlite3.Connection, ent: E.Entity, row: Mapping[str, Any]) -> None:
    cols = list(row)
    quoted = ", ".join(f'"{c}"' for c in cols)
    keys = ", ".join(f'"{c}"' for c in ent.key_columns)
    updates = [c for c in cols if c not in ent.key_columns]
    action = ("DO UPDATE SET " + ", ".join(f'"{c}" = excluded."{c}"' for c in updates)) if updates else "DO NOTHING"
    conn.execute(f'INSERT INTO "{ent.root_table}" ({quoted}) VALUES ({", ".join("?" * len(cols))}) '
                 f'ON CONFLICT({keys}) {action}', [row[c] for c in cols])


def apply_ops_reference(conn: sqlite3.Connection, ops: Sequence[Mapping[str, Any]]) -> None:
    """apply package ops to snapshot"""
    prior = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        conn.execute("PRAGMA defer_foreign_keys = ON")
        try:
            for op in ops:
                kind, ent = op["op"], _entity(op["entity"])
                key = _native_key(ent, op["key"])
                if kind == "UPSERT":
                    data = op["data"]
                    _upsert_root(conn, ent, data[ent.root_table])
                    for child in ent.children:
                        sql, params = E.child_where(ent, child, key)
                        conn.execute(f'DELETE FROM "{child.table}" WHERE {sql}', params)
                    done: set[str] = set()
                    for child in reversed(ent.children):
                        if child.table in done:
                            continue
                        done.add(child.table)
                        payload = data.get(child.table)
                        if payload is None:
                            continue
                        _insert_rows(conn, child.table, [payload] if isinstance(payload, Mapping) else payload)
                elif kind == "RETIRE":
                    conn.execute("UPDATE equipment SET state = 'retired', retired_revision = ?, "
                                 "last_changed_revision = ? WHERE uid = ?", [op["rev"], op["rev"], op["key"]])
                elif kind == "ALIAS":
                    pass                                    # re-points client user rows
                elif kind == "DELETE":
                    for child in ent.children:
                        sql, params = E.child_where(ent, child, key)
                        conn.execute(f'DELETE FROM "{child.table}" WHERE {sql}', params)
                    where, params = E.root_where(ent, key)
                    conn.execute(f'DELETE FROM "{ent.root_table}" WHERE {where}', params)
                else:
                    raise ReleaseError(f"unknown op {kind!r}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ReleaseError(f"foreign_key_check after apply: {violations[:5]}")
    finally:
        conn.isolation_level = prior


def replay(snapshot_r_gz: Path, package: Mapping[str, Any], expected: Mapping[str, str], *,
           tmp_dir: Path) -> None:
    """replay package against snapshot"""
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    work = tmp_dir / f"replay-{package['from_revision']}-{package['to_revision']}-{uuid.uuid4().hex[:8]}.sqlite"
    gunzip_file(Path(snapshot_r_gz), work)
    try:
        conn = sqlite3.connect(work)
        try:
            apply_ops_reference(conn, package["ops"])
        finally:
            conn.close()
        got = table_hashes(work, list(expected))
    finally:
        work.unlink(missing_ok=True)
    differing = [t for t in expected if got.get(t) != expected[t]]
    if differing:
        raise ReplayError(f"replay {package['from_revision']} -> {package['to_revision']} differs in: "
                          f"{', '.join(differing)}")


def check_ledger_grows(prev_snapshot: Path, new_snapshot: Path) -> None:
    """check ledger rows persist unchanged"""
    with _sqlite_ro(prev_snapshot) as old, _sqlite_ro(new_snapshot) as new:
        for table in E.LEDGER_TABLES:
            cols = [c[1] for c in old.execute(f'PRAGMA table_info("{table}")')]
            quoted = ", ".join(f'"{c}"' for c in cols)
            have = set(new.execute(f'SELECT {quoted} FROM "{table}"').fetchall())
            lost = [r for r in old.execute(f'SELECT {quoted} FROM "{table}"') if r not in have]
            if lost:
                raise ReleaseError(f"ledger table {table} lost or changed {len(lost)} row(s): {lost[:3]}")


# icons and packs
class IconSource:
    def __init__(self, seed: Path | None, store: ObjectStore | None):
        self.seed, self.store = seed, store
        self._conn: sqlite3.Connection | None = None
        self._cache: dict[str, bytes] = {}

    def _seed_conn(self) -> sqlite3.Connection | None:
        if self._conn is None and self.seed is not None and Path(self.seed).exists():
            self._conn = sqlite3.connect(f"file:{Path(self.seed).resolve()}?mode=ro", uri=True)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get(self, kind: str, sha: str) -> bytes:
        if sha in self._cache:
            return self._cache[sha]
        conn = self._seed_conn()
        if conn is not None:
            row = conn.execute("SELECT body FROM asset WHERE sha256 = ? AND body IS NOT NULL LIMIT 1",
                               (sha,)).fetchone()
            if row is not None:
                body = bytes(row[0])
                if sha256_bytes(body) != sha:
                    raise ReleaseError(f"icon {sha}: seed asset body does not hash to its sha256")
                self._cache[sha] = body
                return body
        if self.store is not None:
            try:
                body = self.store.get(Layout.pub(Layout.rel_icon(kind, sha)), sha256=sha)
            except ObjectMissing:
                body = None
            except IntegrityError as exc:
                raise ReleaseError(f"icon {sha}: stored object is corrupt: {exc}") from None
            if body is not None:
                self._cache[sha] = body
                return body
        raise ReleaseError(f"icon {sha} ({kind}) is in neither the seed nor the store")


def icon_bytes(seed: Path, store: ObjectStore, sha: str, kind: str) -> bytes:
    src = IconSource(seed, store)
    try:
        return src.get(kind, sha)
    finally:
        src.close()


ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def build_icon_pack(name: str, *, to_revision: int, from_revision: int | None,
                    entries: Sequence[tuple[str, str, bytes]], out: Path) -> tuple[str, int, int]:
    members = sorted(((f"{kind}/{sha}.png", sha, kind, data) for sha, kind, data in entries))
    index = {
        "name": name,
        "pack_type": "base" if from_revision is None else "delta",
        "from_revision": from_revision,
        "to_revision": to_revision,
        "icons": [{"sha256": sha, "kind": kind, "bytes": len(data), "path": path}
                  for path, sha, kind, data in members],
    }
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    def info(member: str) -> zipfile.ZipInfo:
        zi = zipfile.ZipInfo(member, date_time=ZIP_DATE)
        zi.compress_type = zipfile.ZIP_STORED
        zi.external_attr = 0o644 << 16
        zi.create_system = 3
        return zi

    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(info("index.json"), canonical_bytes(index))
        for path, _sha, _kind, data in members:
            zf.writestr(info(path), data)
    return sha256_file(out), out.stat().st_size, len(members)


def read_pack_index(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if not names or names[0] != "index.json":
            raise ContractError(f"{path}: index.json is not the first member")
        return json.loads(zf.read("index.json"))


# contract validation
_SCHEMA_CACHE: dict[Path, Any] = {}


def validate(kind: Literal["current", "manifest", "update_package", "icon_pack"], obj: Any, contract_dir: Path | None) -> None:
    if contract_dir is None:
        return
    import jsonschema
    path = Path(contract_dir) / f"{kind}.schema.json"
    if not path.exists():
        raise ContractError(f"{path}: contract schema is missing")
    schema = _SCHEMA_CACHE.get(path)
    if schema is None:
        schema = _SCHEMA_CACHE[path] = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(obj), key=lambda e: list(e.absolute_path))
    if errors:
        shown = "; ".join(f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message[:200]}"
                          for e in errors[:5])
        raise ContractError(f"{kind} does not match its schema ({len(errors)} error(s)): {shown}")


def check_invariants(manifest: Mapping[str, Any], packages: Mapping[int, Mapping[str, Any]],
                     packs: Mapping[str, Mapping[str, Any]]) -> None:
    """Check manifest invariants."""
    n = manifest["revision"]
    art = manifest["artifacts"]
    minimum = manifest["minimum_revision"]
    # manifest invariants
    if art["snapshot"]["path"] != Layout.rel_snapshot(n):
        raise ContractError("INV-4: snapshot path does not embed the revision")
    if art["lineage"]["path"] != Layout.rel_lineage(n):
        raise ContractError("INV-4: lineage path does not embed the revision")
    base = art["icon_packs"]["base"]
    if base["path"] != Layout.rel_base_pack(n):
        raise ContractError("INV-4: base pack path does not embed the revision")
    froms = [u["from_revision"] for u in art["updates"]]
    for u in art["updates"]:
        if u["path"] != Layout.rel_update(u["from_revision"], n):
            raise ContractError(f"INV-4: update path {u['path']} does not match from_revision {u['from_revision']}")
    if manifest["snapshot_only"]:
        if minimum != n:
            raise ContractError("INV-4: snapshot-only release must have minimum_revision == revision")
        if froms or art["icon_packs"]["deltas"]:
            raise ContractError("INV-4: snapshot-only release carries packages")
    elif froms != list(range(minimum, n)):
        raise ContractError(f"INV-4: updates {froms} are not exactly range({minimum}, {n})")
    # manifest invariants
    for d in art["icon_packs"]["deltas"]:
        if d["from_revision"] not in froms:
            raise ContractError(f"INV-5: delta from {d['from_revision']} has no update package")
        if d["path"] != Layout.rel_delta_pack(d["from_revision"], n):
            raise ContractError(f"INV-5: delta path {d['path']} does not match from_revision")
    # manifest invariants
    if manifest["entity_counts"]["icon"] != manifest["icons"]["count"]:
        raise ContractError("INV-9: entity_counts.icon != icons.count")
    deltas = {d["from_revision"]: d for d in art["icon_packs"]["deltas"]}
    # manifest invariants
    for r, pkg in packages.items():
        _check_package(pkg, n, minimum)
        delta = deltas.get(pkg["from_revision"])
        icons = pkg["icons"]
        if delta is None:
            if icons["added"] or icons["pack"] is not None or icons["pack_sha256"] is not None:
                raise ContractError(f"INV-7: package {r}->{n} lists icons but the manifest has no delta pack")
            continue
        if icons["pack"] != delta["path"] or icons["pack_sha256"] != delta["sha256"]:
            raise ContractError(f"INV-7: package {r}->{n} pack/pack_sha256 differ from the manifest")
        index = packs.get(delta["path"])
        if index is None:
            raise ContractError(f"INV-7: no index.json supplied for {delta['path']}")
        if sorted(icons["added"]) != sorted(i["sha256"] for i in index["icons"]):
            raise ContractError(f"INV-7: package {r}->{n} `added` differs from the pack index")
    # manifest invariants
    for path, index in packs.items():
        name = path.rsplit("/", 1)[-1]
        if index["name"] != name:
            raise ContractError(f"INV-8: index name {index['name']} != {name}")
        if index["to_revision"] != n:
            raise ContractError(f"INV-8: {path} to_revision {index['to_revision']} != {n}")
        for icon in index["icons"]:
            if icon["path"] != f"{icon['kind']}/{icon['sha256']}.png":
                raise ContractError(f"INV-8: {path} member path {icon['path']} != kind/sha256.png")
        if index["pack_type"] == "base" and base["icon_count"] != len(index["icons"]):
            raise ContractError("INV-8: base pack icon_count != len(icons)")


def _check_package(pkg: Mapping[str, Any], n: int, minimum: int) -> None:
    lo, hi = pkg["from_revision"], pkg["to_revision"]
    if not (minimum <= lo < hi == n):
        raise ContractError(f"INV-6: package {lo}->{hi} outside [{minimum}, {n}]")
    seen: set[tuple[str, str, str]] = set()
    retired_at: dict[str, int] = {}
    for i, op in enumerate(pkg["ops"]):
        ident = (op["op"], op["entity"], op["key"])
        if ident in seen:
            raise ContractError(f"INV-6: duplicate op {ident}")
        seen.add(ident)
        if not lo < op["rev"] <= hi:
            raise ContractError(f"INV-6: op {ident} rev {op['rev']} outside ({lo}, {hi}]")
        if op["op"] == "RETIRE":
            retired_at[op["key"]] = i
        if op["op"] == "ALIAS":
            if op["to"] == op["key"]:
                raise ContractError(f"INV-6: ALIAS {op['key']} -> itself")
            if op["key"] in retired_at and retired_at[op["key"]] > i:
                raise ContractError(f"INV-6: ALIAS {op['key']} precedes its RETIRE")
        if op["op"] != "UPSERT":
            continue
        data, key = op["data"], op["key"]
        if op["entity"] == "equipment":
            if data["equipment"].get("uid") != key:
                raise ContractError(f"INV-6: equipment UPSERT {key} carries uid {data['equipment'].get('uid')}")
            for table in ("evolution_edge", "evolution_material"):
                for row in data.get(table, []):
                    if key not in (row.get("from_uid"), row.get("to_uid")):
                        raise ContractError(f"INV-6: {table} row of equipment {key} touches neither endpoint")
        elif op["entity"] == "uid_retired":
            row = data["uid_retired"]
            if row["uid"] != key or not lo < row["since_revision"] <= hi:
                raise ContractError(f"INV-6: uid_retired {key} inconsistent")
        elif op["entity"] == "uid_alias":
            row = data["uid_alias"]
            if row["old_uid"] != key or not lo < row["since_revision"] <= hi:
                raise ContractError(f"INV-6: uid_alias {key} inconsistent")


# revision window
def _release_revisions(history: Path | None) -> set[int]:
    if history is None or not Path(history).exists():
        return set()
    with _duck(history) as conn:
        if not _has_table(conn, "release"):
            return set()
        return {int(r[0]) for r in conn.execute("SELECT revision FROM release").fetchall()}


def next_revision(store: ObjectStore, history: Path | None) -> int:
    seen = {0}
    try:
        seen.add(int(json.loads(store.get(Layout.CURRENT))["revision"]))
    except ObjectMissing:
        pass
    for info in store.list(Layout.pub("manifests/")):
        n = Layout.parse_manifest_key(info.key)
        if n is not None:
            seen.add(n)
    seen |= _release_revisions(history)
    return max(seen) + 1


class _Published:
    """cached store listing"""
    def __init__(self, store: ObjectStore):
        self.store = store
        self.manifest_revs = {n for i in store.list(Layout.pub("manifests/"))
                              if (n := Layout.parse_manifest_key(i.key)) is not None}
        self.snapshot_revs = {n for i in store.list(Layout.pub("snapshots/"))
                              if (n := Layout.parse_snapshot_key(i.key)) is not None}
        self._manifests: dict[int, dict] = {}

    def manifest(self, r: int) -> dict[str, Any]:
        if r not in self._manifests:
            self._manifests[r] = json.loads(self.store.get(Layout.pub(Layout.rel_manifest(r))))
        return self._manifests[r]


def _qualifies(r: int, published: _Published, releases: set[int], *, dataset_schema_version: int,
               catalog_version: str) -> bool:
    if r not in releases or r not in published.snapshot_revs or r not in published.manifest_revs:
        return False
    m = published.manifest(r)
    return (m.get("dataset_schema_version") == dataset_schema_version
            and m.get("build", {}).get("catalog_version") == catalog_version)


def supported_from_revisions(store: ObjectStore, history: Path | None, *, revision: int,
                             dataset_schema_version: int, catalog_version: str, window: int,
                             size_ok: Callable[[int], bool] | None = None,
                             published: _Published | None = None) -> list[int]:
    """contiguous from-revisions, ascending"""
    published = published or _Published(store)
    releases = _release_revisions(history)
    kept: list[int] = []
    for r in range(revision - 1, max(1, revision - window) - 1, -1):
        if not _qualifies(r, published, releases, dataset_schema_version=dataset_schema_version,
                          catalog_version=catalog_version):
            break
        if size_ok is not None and not size_ok(r):
            break
        kept.append(r)
    return sorted(kept)


def snapshot_only_reasons(*, first_release: bool, forced: bool, dataset_schema_changed: bool,
                          catalog_changed: bool, package_too_big: bool, no_base: bool = False) -> list[str]:
    flags = {"package_size": package_too_big, "dataset_schema_version": dataset_schema_changed,
             "catalog_version": catalog_changed, "forced": forced, "first_release": first_release,
             "no_base": no_base}
    return [r for r in SNAPSHOT_ONLY_REASONS if flags[r]]


@dataclass(frozen=True)
class BundleFile:
    path: str
    sha256: str
    bytes: int
    content_type: str


@dataclass(frozen=True)
class Bundle:
    root: Path
    revision: int
    run_id: str
    created_at: str
    dataset_schema_version: int
    api_schema_version: int
    minimum_revision: int
    snapshot_only_reasons: tuple[str, ...]
    manifest_sha256: str
    dirty: bool
    gate: Mapping[str, Any]
    replay_from: tuple[int, ...]
    table_hashes: Mapping[str, str]
    files: tuple[BundleFile, ...]

    @classmethod
    def load(cls, root: Path) -> Bundle:
        """load bundle, re-hash files"""
        root = Path(root)
        try:
            m = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ReleaseError(f"{root}: no bundle.json") from None
        files = []
        for f in m["files"]:
            path = root / f["path"]
            if not path.is_file():
                raise IntegrityError(f"{f['path']}: missing from the bundle")
            if path.stat().st_size != f["bytes"] or sha256_file(path) != f["sha256"]:
                raise IntegrityError(f"{f['path']}: does not match bundle.json")
            files.append(BundleFile(f["path"], f["sha256"], f["bytes"], f["content_type"]))
        return cls(root, m["revision"], m["run_id"], m["created_at"], m["dataset_schema_version"],
                   m["api_schema_version"], m["minimum_revision"], tuple(m["snapshot_only_reasons"]),
                   m["manifest_sha256"], bool(m["dirty"]), m["gate"], tuple(m["replay_from"]),
                   dict(m["table_hashes"]), tuple(files))

    def pub_files(self) -> list[BundleFile]:
        return [f for f in self.files if f.path.startswith("pub/")]

    def build_files(self) -> list[BundleFile]:
        return [f for f in self.files if f.path.startswith("build/")]

    def manifest(self) -> dict[str, Any]:
        return json.loads((self.root / "pub" / Layout.rel_manifest(self.revision)).read_text(encoding="utf-8"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "bundle_version": BUNDLE_VERSION, "revision": self.revision, "run_id": self.run_id,
            "created_at": self.created_at, "dataset_schema_version": self.dataset_schema_version,
            "api_schema_version": self.api_schema_version, "minimum_revision": self.minimum_revision,
            "snapshot_only_reasons": list(self.snapshot_only_reasons), "manifest_sha256": self.manifest_sha256,
            "dirty": self.dirty, "gate": dict(self.gate), "replay_from": list(self.replay_from),
            "table_hashes": dict(self.table_hashes),
            "files": [{"path": f.path, "sha256": f.sha256, "bytes": f.bytes, "content_type": f.content_type}
                      for f in self.files],
        }


def _bundle_files(root: Path) -> tuple[BundleFile, ...]:
    out = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel == "bundle.json" or not rel.startswith(("pub/", "build/")):
            continue
        out.append(BundleFile(rel, sha256_file(path), path.stat().st_size, content_type_of(rel)))
    return tuple(out)


# build release
def _read_transform_run(work: Path, history: Path) -> dict[str, Any]:
    marker = work / "transform_run.json"
    try:
        run = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReleaseError(f"{marker}: missing (run the model stage first)") from None
    run_id = run.get("run_id", run.get("transform_run_id"))
    if run_id is None:
        raise ReleaseError(f"{marker}: no run_id")
    with _duck(history) as conn:
        if not _has_table(conn, "transform_run"):
            raise ReleaseError("history: table transform_run is missing")
        rows = _dicts(conn, "SELECT * FROM transform_run WHERE run_id = ?", [str(run_id)])
    if not rows:
        raise ReleaseError(f"history: no transform_run row {run_id!r}")
    row = rows[0]
    for col in ("parser_version", "catalog_version", "catalog_commit", "ended_at"):
        if col not in row:
            raise ReleaseError(f"history.transform_run: column {col} is missing")
    return row


def _seed_facts(seed: Path, shipped_uids: set[str]) -> dict[str, Any]:
    with _sqlite_ro(seed) as conn:
        run = conn.execute("SELECT run_id FROM crawl_run ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()
        latest = None
        for item_id, changed in conn.execute("SELECT item_id, changed_at FROM page WHERE kind = 'detail'"):
            if item_id in shipped_uids and changed and (latest is None or changed > latest):
                latest = changed
    return {"crawl_run_id": None if run is None else str(run[0]),
            "source_last_updated": latest if latest and _TS_RE.match(latest) else None}


def _lineage_items(history: Path, mart: Path) -> list[dict[str, Any]]:
    with _duck(mart) as conn:
        equipment = {r[0]: r[1] for r in conn.execute("SELECT uid, last_changed_revision FROM equipment").fetchall()}
    with _duck(history) as conn:
        if not _has_table(conn, "source_lineage"):
            raise ReleaseError("history: table source_lineage is missing")
        have = set(_columns(conn, "source_lineage"))
        want = ["uid", "source_url", "source_hash", "etag", "retrieved_at", "crawl_run_id", "keep_reason",
                "entry_kind", "depth", "parent_id"]
        select = ", ".join(c if c in have else f"NULL AS {c}" for c in want)
        where = "WHERE dbt_valid_to IS NULL" if "dbt_valid_to" in have else ""
        lineage = {r["uid"]: r for r in _dicts(conn, f"SELECT {select} FROM source_lineage {where}")}
        hashes: dict[str, str | None] = {}
        if _has_table(conn, "entity_current"):
            hashes = {r[0]: r[1] for r in conn.execute(
                "SELECT entity_key, row_hash FROM entity_current WHERE entity_type = 'equipment'").fetchall()}
    items = []
    for uid in sorted(equipment):
        row = dict(lineage.get(uid) or {c: None for c in want})
        row["uid"] = uid
        for k, v in list(row.items()):
            if isinstance(v, (datetime,)):
                row[k] = utc_text(v)
        row["row_hash"] = hashes.get(uid)
        row["last_changed_revision"] = equipment[uid]
        items.append(row)
    return items


def _heartbeat_revision(store: ObjectStore) -> int:
    try:
        return int(json.loads(store.get(Layout.CURRENT))["revision"])
    except ObjectMissing:
        return 0


def build_release(inputs: ReleaseInputs) -> ReleaseDecision:
    work = Path(inputs.work_dir)
    n = inputs.revision
    store = inputs.store
    mart, history, seed = work / "mart.duckdb", work / "history" / "current.duckdb", work / "seed.sqlite"
    for path in (mart, history, seed):
        if not path.exists():
            raise ReleaseError(f"{path}: missing")
    release_root = work / "release"
    release_root.mkdir(parents=True, exist_ok=True)
    decision_path = release_root / "decision.json"

    def finish(decision: ReleaseDecision) -> ReleaseDecision:
        decision_path.write_text(json.dumps(decision.to_mapping(work), sort_keys=True, indent=2) + "\n",
                                 encoding="utf-8")
        return decision

    gate = GateSummary.load(work / "gate.json")
    if gate.level == "error":
        raise ReleaseError("gate level is error: no release")

    # empty changelog
    changes_n = read_changes(history, n - 1, n)
    if not changes_n:
        if gate.level == "blocker":
            raise ReleaseError("gate is blocker with an empty change log: nothing to approve, failing closed")
        current_rev = _heartbeat_revision(store)
        return finish(ReleaseDecision("heartbeat", current_rev, gate.level, None, "no changes"))

    bundle_dir = release_root / str(n)
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)
    pub = bundle_dir / "pub"
    tmp = bundle_dir / "tmp"
    tmp.mkdir()

    transform = _read_transform_run(work, history)

    # shipped tables and snapshot
    shipped = shipped_tables(inputs.schema_text)
    check_entities(mart, shipped)
    for ent_type in {c.entity_type for c in changes_n}:
        _entity(ent_type)
    catalog_version = str(transform["catalog_version"])
    published = _Published(store)
    releases = _release_revisions(history)
    prior = sorted(r for r in (releases | published.manifest_revs) if r < n)
    prev = max(prior) if prior else None
    prev_dir = tmp / "prev"

    def previous_snapshot_gz(r: int) -> Path:
        gz = prev_dir / f"unison-{r}.sqlite.gz"
        if not gz.exists():
            expect = published.manifest(r)["artifacts"]["snapshot"]["sha256"]
            store.get_file(Layout.pub(Layout.rel_snapshot(r)), gz, sha256=expect)
        return gz

    def previous_snapshot(r: int) -> Path:
        """the same, gunzipped"""
        sqlite_path = prev_dir / f"unison-{r}.sqlite"
        if not sqlite_path.exists():
            gunzip_file(previous_snapshot_gz(r), sqlite_path)
        return sqlite_path

    # newest earlier snapshot feeds carry-forward
    carry_base = max((r for r in published.snapshot_revs & published.manifest_revs if r < n), default=None)
    parse_failures = read_parse_failures(mart)
    snap = build_snapshot(
        mart, bundle_dir, revision=n, dataset_schema_version=inputs.dataset_schema_version,
        schema_text=inputs.schema_text,
        prev_snapshot=previous_snapshot(carry_base) if carry_base is not None and parse_failures else None)

    current_rev = _heartbeat_revision(store)
    if current_rev and current_rev in published.snapshot_revs:
        check_ledger_grows(previous_snapshot(current_rev), snap.sqlite_path)

    # window, packages, icon packs, replay
    icons_rows = _read_icons(mart)
    if snap.carried_shared.get("icon"):             # carried icons are in snapshot
        with _sqlite_ro(snap.sqlite_path) as conn:
            icons_rows = {**{r[0]: r[1] for r in conn.execute("SELECT sha256, kind FROM icon")}, **icons_rows}
    source = IconSource(seed, store)
    try:
        existing_icons = {i.key for i in store.list(Layout.pub("icons/"))}
        # store-missing icons ship in bundle
        for sha, kind in sorted(icons_rows.items()):
            key = Layout.pub(Layout.rel_icon(kind, sha))
            if key not in existing_icons:
                dest = bundle_dir / key
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(source.get(kind, sha))

        prev_manifest = published.manifest(prev) if prev is not None and prev in published.manifest_revs else None
        first_release = prev is None
        dsv_changed = bool(prev_manifest) and prev_manifest.get("dataset_schema_version") != inputs.dataset_schema_version
        cat_changed = bool(prev_manifest) and prev_manifest.get("build", {}).get("catalog_version") != catalog_version
        packages: dict[int, dict[str, Any]] = {}
        updates_refs: list[dict[str, Any]] = []
        delta_refs: list[dict[str, Any]] = []
        pack_indexes: dict[str, dict[str, Any]] = {}
        package_too_big = False
        kept: list[int] = []
        aliases = alias_end(mart)

        if not inputs.force_snapshot:
            def build_one(r: int) -> bool:
                nonlocal package_too_big
                ops = net_ops(read_changes(history, r, n), mart=mart, alias_end=aliases)
                with _sqlite_ro(previous_snapshot(r)) as old:
                    old_icons = {x[0] for x in old.execute("SELECT sha256 FROM icon")}
                added = sorted(set(icons_rows) - old_icons)
                pack_rel = pack_sha = None
                if added:
                    pack_rel = Layout.rel_delta_pack(r, n)
                    entries = [(sha, icons_rows[sha], source.get(icons_rows[sha], sha)) for sha in added]
                    pack_sha, pack_bytes, count = build_icon_pack(
                        pack_rel.rsplit("/", 1)[-1], to_revision=n, from_revision=r, entries=entries,
                        out=pub / pack_rel)
                    delta_refs.append({"from_revision": r, "path": pack_rel, "sha256": pack_sha,
                                       "bytes": pack_bytes, "icon_count": count})
                    pack_indexes[pack_rel] = read_pack_index(pub / pack_rel)
                pkg = build_package(from_revision=r, to_revision=n,
                                    dataset_schema_version=inputs.dataset_schema_version, ops=ops,
                                    icons={"pack": pack_rel, "pack_sha256": pack_sha, "added": added})
                rel = Layout.rel_update(r, n)
                sha, size = write_json_gz(pkg, pub / rel)
                if size > inputs.snapshot_only_ratio * snap.bytes:
                    (pub / rel).unlink()
                    if added:
                        (pub / pack_rel).unlink()
                        delta_refs.pop()
                        pack_indexes.pop(pack_rel)
                    if r == prev:
                        package_too_big = True
                    return False
                packages[r] = pkg
                updates_refs.append({"from_revision": r, "path": rel, "sha256": sha, "bytes": size})
                return True

            kept = supported_from_revisions(
                store, history, revision=n, dataset_schema_version=inputs.dataset_schema_version,
                catalog_version=catalog_version, window=inputs.window, size_ok=build_one, published=published)

        reasons = snapshot_only_reasons(
            first_release=first_release, forced=inputs.force_snapshot, dataset_schema_changed=dsv_changed,
            catalog_changed=cat_changed, package_too_big=package_too_big)
        if not kept and not reasons:
            reasons = ["no_base"]                                # automatic, never operator error
        snapshot_only = not kept
        minimum_revision = n if snapshot_only else min(kept)

        for r in kept:                                           # every kept package must replay exactly
            replay(previous_snapshot_gz(r), packages[r], snap.table_hashes, tmp_dir=tmp / "replay")

        base_rel = Layout.rel_base_pack(n)
        base_entries = [(sha, kind, source.get(kind, sha)) for sha, kind in sorted(icons_rows.items())]
        base_sha, base_bytes, base_count = build_icon_pack(
            base_rel.rsplit("/", 1)[-1], to_revision=n, from_revision=None, entries=base_entries, out=pub / base_rel)
        pack_indexes[base_rel] = read_pack_index(pub / base_rel)
    finally:
        source.close()

    updates_refs.sort(key=lambda u: u["from_revision"])
    delta_refs.sort(key=lambda d: d["from_revision"])

    # lineage, manifest, contract validation
    now = inputs.clock()
    items = _lineage_items(history, mart)
    facts = _seed_facts(seed, {i["uid"] for i in items})
    generated_at = utc_text(now)
    dataset_generated_at = str(transform["ended_at"])
    if not _TS_RE.match(dataset_generated_at):
        dataset_generated_at = generated_at
    crawl_run_id = facts["crawl_run_id"]
    if crawl_run_id is None and transform.get("crawl_run_id") is not None:
        crawl_run_id = str(transform["crawl_run_id"])
    if crawl_run_id is None:
        raise ReleaseError("seed.crawl_run has no rows and transform_run has no crawl_run_id")
    transform_run_id = str(transform["run_id"])
    if not _RUN_ID_RE.match(transform_run_id) or not _RUN_ID_RE.match(crawl_run_id):
        raise ContractError(f"run ids must match {_RUN_ID_RE.pattern}: {transform_run_id!r}, {crawl_run_id!r}")

    failed = {f["uid"]: f for f in parse_failures}
    carried_forward = {                                          # lineage only, not manifest
        "from_revision": carry_base if snap.carried else None,
        "uids": [{"uid": uid, "source_hash": failed[uid]["source_hash"],
                  "parser_version": failed[uid]["parser_version"], "error": failed[uid]["error"]}
                 for uid in snap.carried],
        "shared_rows": dict(snap.carried_shared),     # shared rows copied per table
        "warnings": list(snap.carry_warnings),
    }
    lineage = {
        "revision": n, "generated_at": generated_at,
        "build": {"build_commit": inputs.build.build_commit, "dirty": inputs.build.dirty,
                  "run_id": inputs.build.run_id, "dbt_invocation_id": transform.get("dbt_invocation_id"),
                  "dbt_manifest_sha256": transform.get("dbt_manifest_sha256"), "runtime": dict(inputs.build.runtime)},
        "transform_run": {k: (utc_text(v) if isinstance(v, datetime) else v) for k, v in transform.items()
                          if k in ("run_id", "code_commit", "parser_version", "catalog_version", "catalog_commit",
                                   "policy_version", "dbt_invocation_id", "dbt_manifest_sha256", "started_at",
                                   "ended_at")},
        "snapshot": {"sqlite_sha256": snap.sqlite_sha256, "sqlite_bytes": snap.sqlite_bytes,
                     "table_hashes": dict(snap.table_hashes)},
        "changes": {op: sum(1 for c in changes_n if c.operation == op) for op in OPERATIONS},
        "items": items,
        "carried_forward": carried_forward,
        "icons": [{"kind": kind, "sha256": sha} for sha, kind in sorted(icons_rows.items(), key=lambda t: (t[1], t[0]))],
    }
    lineage_rel = Layout.rel_lineage(n)
    lineage_sha, lineage_bytes = write_json_gz(lineage, pub / lineage_rel)

    runtime = {k: inputs.build.runtime.get(k, "absent") for k in ("python", "duckdb", "dbt_core", "dbt_duckdb")}
    manifest = {
        "revision": n,
        "dataset_schema_version": inputs.dataset_schema_version,
        "api_schema_version": inputs.api_schema_version,
        "minimum_revision": minimum_revision,
        "snapshot_only": snapshot_only,
        "snapshot_only_reasons": reasons if snapshot_only else [],
        "freshness": {"source_last_updated": facts["source_last_updated"] or dataset_generated_at,
                      "dataset_generated_at": dataset_generated_at},
        "build": {
            "build_commit": _commit(inputs.build.build_commit),
            "transform_run_id": transform_run_id,
            "crawl_run_id": crawl_run_id,
            "seed_sha256": sha256_file(seed),
            "parser_version": int(transform["parser_version"]),
            "catalog_version": catalog_version,
            "catalog_commit": _commit(transform["catalog_commit"]),
            "policy_version": gate.policy_version,
            "policy_commit": _commit(gate.policy_commit),
            "runtime": runtime,
        },
        "entity_counts": dict(snap.counts),
        "artifacts": {
            "snapshot": {"path": Layout.rel_snapshot(n), "sha256": snap.sha256, "bytes": snap.bytes},
            "updates": updates_refs,
            "icon_packs": {"base": {"path": base_rel, "sha256": base_sha, "bytes": base_bytes,
                                    "icon_count": base_count},
                           "deltas": delta_refs},
            "lineage": {"path": lineage_rel, "sha256": lineage_sha, "bytes": lineage_bytes},
        },
        "icons": {"count": snap.counts["icon"], "withdrawn": []},
        "gate": {"level": gate.level, "warnings": gate.warnings()},
    }
    validate("manifest", manifest, inputs.contract_dir)
    for pkg in packages.values():
        validate("update_package", pkg, inputs.contract_dir)
    for index in pack_indexes.values():
        validate("icon_pack", index, inputs.contract_dir)
    check_invariants(manifest, packages, pack_indexes)
    manifest_sha, _ = write_json(manifest, pub / Layout.rel_manifest(n))

    # the bundle files and state
    shutil.rmtree(tmp)
    build_dir = bundle_dir / "build"
    build_dir.mkdir()
    shutil.copy2(seed, build_dir / "seed.sqlite")
    shutil.copy2(history, build_dir / "history.duckdb")
    bundle = Bundle(
        root=bundle_dir, revision=n, run_id=inputs.build.run_id, created_at=generated_at,
        dataset_schema_version=inputs.dataset_schema_version, api_schema_version=inputs.api_schema_version,
        minimum_revision=minimum_revision, snapshot_only_reasons=tuple(manifest["snapshot_only_reasons"]),
        manifest_sha256=manifest_sha, dirty=inputs.build.dirty,
        gate={"level": gate.level, "policy_version": gate.policy_version},
        replay_from=tuple(sorted(kept)), table_hashes=dict(snap.table_hashes), files=_bundle_files(bundle_dir))
    (bundle_dir / "bundle.json").write_text(
        json.dumps(bundle.to_mapping(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    action: Literal["publish", "hold"] = "hold" if gate.level == "blocker" else "publish"
    reason = f"{len(changes_n)} changes; {len(kept)} packages"
    if snap.carried:
        reason += f"; {len(snap.carried)} uid(s) carried forward (parse failures)"
    return finish(ReleaseDecision(action, n, gate.level, bundle_dir, reason, tuple(snap.carried)))


def _read_icons(mart: Path) -> dict[str, str]:
    with _duck(mart) as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT sha256, kind FROM icon").fetchall()}


__all__ = [
    "LOGICAL_REFS",
    "PACKAGE_WINDOW",
    "SNAPSHOT_ONLY_RATIO",
    "SNAPSHOT_ONLY_REASONS",
    "BuildInfo",
    "Bundle",
    "BundleFile",
    "Change",
    "ContractError",
    "GateSummary",
    "IconSource",
    "ReleaseDecision",
    "ReleaseError",
    "ReleaseInputs",
    "ReplayError",
    "SnapshotInfo",
    "aggregate_data",
    "alias_end",
    "apply_ops_reference",
    "build_icon_pack",
    "build_package",
    "build_release",
    "build_snapshot",
    "check_entities",
    "check_invariants",
    "check_ledger_grows",
    "duck_sql",
    "icon_bytes",
    "net_ops",
    "next_revision",
    "read_changes",
    "read_parse_failures",
    "reference_graph",
    "replay",
    "shipped_tables",
    "snapshot_only_reasons",
    "supported_from_revisions",
    "table_hash",
    "table_hashes",
    "utc_now",
    "utc_text",
    "validate",
]
