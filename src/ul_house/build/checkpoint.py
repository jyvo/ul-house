"""archive closed history rows"""
from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from ul_house.build.release import utc_now, utc_text
from ul_house.build.store import Layout, ObjectMissing, ObjectStore, put_immutable, sha256_file, verify_object
from ul_house.sync.verify import canonical_bytes

ARCHIVE_NEVER = ("uid_retired", "uid_alias", "alias_candidate", "alias_rejected", "transform_run", "release")
OBS_DAYS = 90
_TS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CheckpointError(Exception):
    pass


@dataclass(frozen=True)
class ArchiveRule:
    table: str
    predicate: str              # SQL predicate


@dataclass(frozen=True)
class CheckpointResult:
    checkpoint_id: str
    archive_name: str | None
    archive_sha256: str | None
    current_sha256: str
    counts: Mapping[str, Mapping[str, int]]         # per-table row counts


def _tables(conn: duckdb.DuckDBPyConnection, db: str = "main") -> dict[str, dict[str, str]]:
    """column types per base table"""
    out: dict[str, dict[str, str]] = {}
    rows = conn.execute(
        "SELECT t.table_name, c.column_name, c.data_type FROM information_schema.tables t "
        "JOIN information_schema.columns c ON c.table_schema = t.table_schema AND c.table_name = t.table_name "
        "WHERE t.table_schema = 'main' AND t.table_type = 'BASE TABLE' AND t.table_catalog = current_database() "
        "ORDER BY t.table_name, c.ordinal_position").fetchall()
    for table, column, dtype in rows:
        out.setdefault(table, {})[column] = str(dtype).upper()
    return out


def _time_before(column: str, dtype: str, cutoff_ts: str) -> str:
    if not _TS.match(cutoff_ts):
        raise CheckpointError(f"bad cutoff {cutoff_ts!r}")
    if dtype.startswith("TIMESTAMP") or dtype == "DATE":
        return f"{column} < TIMESTAMP '{cutoff_ts[:10]} {cutoff_ts[11:19]}'"
    return f"{column} < '{cutoff_ts}'"


def archive_rules(conn: duckdb.DuckDBPyConnection, *, cutoff_ts: str, cutoff_revision: int) -> list[ArchiveRule]:
    rules = []
    for table, columns in _tables(conn).items():
        if table in ARCHIVE_NEVER or not _NAME.match(table):
            continue
        if "dbt_valid_to" in columns:
            rules.append(ArchiveRule(table, "dbt_valid_to IS NOT NULL AND "
                                     + _time_before("dbt_valid_to", columns["dbt_valid_to"], cutoff_ts)))
        elif table in ("dataset_changes", "row_version") and "revision" in columns:
            rules.append(ArchiveRule(table, f"revision < {int(cutoff_revision)}"))
        elif table.startswith("obs_") and "observed_at" in columns:
            rules.append(ArchiveRule(table, _time_before("observed_at", columns["observed_at"], cutoff_ts)))
    return rules


def reconcile(counts: Mapping[str, Mapping[str, int]]) -> None:
    """check row counts reconcile"""
    for table, c in counts.items():
        if c["before"] != c["after"] + c["archived"]:
            raise CheckpointError(
                f"{table}: {c['before']} rows before != {c['after']} kept + {c['archived']} archived")


def _count(conn: duckdb.DuckDBPyConnection, table: str, db: str | None = None) -> int:
    ref = f'{db}."{table}"' if db else f'main."{table}"'
    return conn.execute(f"SELECT count(*) FROM {ref}").fetchone()[0]


def _archive_name(store: ObjectStore, when: datetime) -> str:
    base = f"{when.year}-Q{(when.month - 1) // 3 + 1}"
    name, n = base, 1
    while store.head(Layout.archive(name)) is not None:
        n += 1
        name = f"{base}-{n}"
    return name


def run_checkpoint(store: ObjectStore, work_dir: Path, *, clock: Callable[[], datetime] = utc_now,
                   obs_days: int = OBS_DAYS) -> CheckpointResult:
    now = clock().astimezone(UTC)
    cid = now.strftime("%Y%m%dT%H%M%SZ")
    cutoff_ts = utc_text(now - timedelta(days=obs_days))
    work = Path(work_dir) / "checkpoint"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    try:
        current = json.loads(store.get(Layout.CURRENT))
    except ObjectMissing:
        current = None
    cutoff_revision = int(current["minimum_revision"]) if current else 0
    revision = int(current["revision"]) if current else 0

    # copy current as backup
    db_path = work / "current.duckdb"
    store.get_file(Layout.HISTORY_CURRENT, db_path)
    put_immutable(store, Layout.history_backup(cid), db_path, content_type="application/octet-stream")

    # move closed rows to archive
    arc_path = work / "archive.duckdb"
    conn = duckdb.connect(str(db_path))
    counts: dict[str, dict[str, int]] = {}
    try:
        tables = _tables(conn)
        rules = archive_rules(conn, cutoff_ts=cutoff_ts, cutoff_revision=cutoff_revision)
        for table in tables:
            counts[table] = {"before": _count(conn, table), "after": 0, "archived": 0}
        conn.execute(f"ATTACH '{arc_path}' AS arc")
        for rule in rules:
            matching = conn.execute(f'SELECT count(*) FROM main."{rule.table}" WHERE {rule.predicate}').fetchone()[0]
            conn.execute("BEGIN")
            conn.execute(f'CREATE TABLE arc."{rule.table}" AS SELECT * FROM main."{rule.table}" WHERE {rule.predicate}')
            conn.execute("COMMIT")
            counts[rule.table]["archived"] = _count(conn, rule.table, "arc")
            if counts[rule.table]["archived"] != matching:
                raise CheckpointError(f"{rule.table}: the archive holds {counts[rule.table]['archived']} rows "
                                      f"but the rule matched {matching}")
        for rule in rules:
            conn.execute("BEGIN")
            conn.execute(f'DELETE FROM main."{rule.table}" WHERE {rule.predicate}')
            conn.execute("COMMIT")
        for table in tables:
            counts[table]["after"] = _count(conn, table)
        # reconcile before upload
        reconcile(counts)
        archived_rows = {t: c["archived"] for t, c in counts.items() if c["archived"]}
        conn.execute("CHECKPOINT")
        conn.execute("DETACH arc")
        # rewrite current into fresh file
        fresh_path = work / "fresh.duckdb"
        conn.execute(f"ATTACH '{fresh_path}' AS fresh")
        source_db = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(f'COPY FROM DATABASE "{source_db}" TO fresh')
        for table in tables:
            if _count(conn, table, "fresh") != counts[table]["after"]:
                raise CheckpointError(f"{table}: rewritten history has a different row count")
        conn.execute("DETACH fresh")
    except CheckpointError:
        raise
    except Exception as exc:
        raise CheckpointError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()

    # upload archive if non-empty
    archive_name = archive_sha = None
    if archived_rows:
        archive_name = _archive_name(store, now)
        info, _ = put_immutable(store, Layout.archive(archive_name), arc_path, content_type="application/octet-stream")
        archive_sha = info.sha256 or sha256_file(arc_path)
        verify_object(store, Layout.archive(archive_name), archive_sha)

    # upload rewritten current
    current_sha = sha256_file(fresh_path)
    store.put_file(Layout.HISTORY_CURRENT, fresh_path, content_type="application/octet-stream")
    verify_object(store, Layout.HISTORY_CURRENT, current_sha)

    # checkpoint file last
    try:
        previous = json.loads(store.get(Layout.CHECKPOINT)).get("archives", [])
    except ObjectMissing:
        previous = []
    archives: list[dict[str, Any]] = list(previous)
    if archive_name is not None:
        archives.append({"name": f"archived/{archive_name}.duckdb", "sha256": archive_sha, "row_counts": archived_rows})
    doc = {"checkpoint_id": cid, "revision": revision, "cutoff": cutoff_ts, "cutoff_revision": cutoff_revision,
           "current_sha256": current_sha, "archives": archives}
    store.put(Layout.CHECKPOINT, canonical_bytes(doc), content_type="application/json")
    return CheckpointResult(cid, archive_name, archive_sha, current_sha, counts)


__all__ = [
    "ARCHIVE_NEVER",
    "OBS_DAYS",
    "ArchiveRule",
    "CheckpointError",
    "CheckpointResult",
    "archive_rules",
    "reconcile",
    "run_checkpoint",
]
