"""shared harness for tests/test_dbt.py and tests/violations/: run whole transform on a copy of mini seed (offline_dbt fixture)
    copy the seed -> mutate -> load.load_seed -> dbt.run_dbt (child process) -> gate.decide
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import sqlite3
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from mini_seed import MINI_CLOCK, fake_png, mini_scope
from ul_house import rows as R
from ul_house.build import dbt as wrapper
from ul_house.build import gate, load

ANALYTICS = wrapper.ANALYTICS_DIR
CODE_COMMIT = "t" * 40

RELEASE_DDL = """
CREATE TABLE IF NOT EXISTS release (
  revision BIGINT PRIMARY KEY, transform_run_id VARCHAR NOT NULL, crawl_run_id VARCHAR,
  seed_sha256 VARCHAR NOT NULL, snapshot_sha256 VARCHAR NOT NULL, package_shas VARCHAR NOT NULL,
  icon_pack_shas VARCHAR NOT NULL, manifest_sha256 VARCHAR NOT NULL, signature VARCHAR NOT NULL,
  published_at VARCHAR NOT NULL, minimum_revision BIGINT NOT NULL, snapshot_only BOOLEAN NOT NULL,
  gate_level VARCHAR NOT NULL, run_id VARCHAR NOT NULL);
"""


_SCRATCH: list[Path] = []


def scratch_root() -> Path:
    if not _SCRATCH:
        import atexit
        import tempfile
        root = Path(tempfile.mkdtemp(prefix="ul_house_dbt_"))
        atexit.register(shutil.rmtree, root, True)
        _SCRATCH.append(root)
    return _SCRATCH[0]


def scratch(name: str) -> Path:
    path = scratch_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


PREVIOUS_RUN_AT = "2026-10-20T12:00:00Z"            # > 7 days after mini seed's first_seen


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def copy_seed(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "seed.sqlite"
    shutil.copyfile(src, dst)
    return dst


def edit_seed(seed: Path, *statements: str) -> None:
    con = sqlite3.connect(seed)
    try:
        for statement in statements:
            con.execute(statement)
        con.commit()
    finally:
        con.close()


def garbage_page(seed: Path, uid: str, body: bytes = b"\x00 not an equipment page \xff") -> None:
    edit_seed(seed, f"UPDATE page SET html = x'{zlib.compress(body).hex()}', "
                    f"html_sha256 = '{hashlib.sha256(body).hexdigest()}' WHERE item_id = '{uid}'")


def clone_item(seed: Path, src: str, new: str, *, discovered_at: str = "2026-10-06T00:00:00Z",
               icon: bool = True) -> None:
    con = sqlite3.connect(seed)
    try:
        (url,) = con.execute("SELECT url FROM page WHERE item_id = ?", [src]).fetchone()
        new_url = url.replace(src, new)
        columns = [c[1] for c in con.execute("PRAGMA table_info(page)").fetchall()]
        row = dict(zip(columns, con.execute("SELECT * FROM page WHERE item_id = ?", [src]).fetchone()))
        row.update(url=new_url, item_id=new, fetched_at=discovered_at)
        con.execute(f"INSERT INTO page ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                    [row[c] for c in columns])
        (run_id,) = con.execute("SELECT value FROM meta WHERE key = 'crawl.last_complete_run'").fetchone()
        con.execute("INSERT INTO frontier (item_id, source, entry_kind, keep_reason, depth, parent_id, state, "
                    "attempts, last_error, discovered_at, updated_at, decided_run_id) "
                    "VALUES (?, 'manual', 'catalog', 'cost_band', 0, NULL, 'done', 1, NULL, ?, ?, ?)",
                    [new, discovered_at, discovered_at, int(run_id)])
        if icon:
            body = fake_png(new)
            columns = [c[1] for c in con.execute("PRAGMA table_info(asset)").fetchall()]
            src_row = dict(zip(columns, con.execute("SELECT * FROM asset WHERE item_id = ?", [src]).fetchone()))
            src_row.update(url=src_row["url"].replace(src, new), item_id=new, body=body,
                           sha256=hashlib.sha256(body).hexdigest())
            con.execute(f"INSERT INTO asset ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                        [src_row[c] for c in columns])
        con.commit()
    finally:
        con.close()


# mutation
def patch(records: R.Records, table: str, *, where: Callable | None = None, drop: bool = False,
          append: tuple = (), **changes) -> R.Records:
    rows = []
    for rec in getattr(records, table):
        if where is not None and where(rec):
            if drop:
                continue
            rec = dataclasses.replace(rec, **changes)
        rows.append(rec)
    rows.extend(append)
    return dataclasses.replace(records, **{table: rows})


# proj copy
def copy_project(dst: Path) -> Path:
    shutil.copytree(ANALYTICS, dst, ignore=shutil.ignore_patterns("target", "logs", "__pycache__"))
    return dst


def append_csv(project: Path, name: str, *lines: str) -> None:
    path = project / "reference" / name
    text = path.read_text()
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "".join(line + "\n" for line in lines))


def policy_with(tmp: Path, mutate: Callable[[dict], None]) -> Path:
    import tomllib
    data = tomllib.loads(gate.POLICY_PATH.read_text())
    mutate(data)
    out = tmp / "policy_override.toml"
    lines = []
    for name, body in data["check"].items():
        lines.append(f"[check.{name}]")
        for key, table in body.items():
            inner = ", ".join(f'{k} = {json.dumps(v) if isinstance(v, str) else repr(v)}' for k, v in table.items())
            lines.append(f"{key} = {{ {inner} }}")
        lines.append("")
    out.write_text("\n".join(lines))
    return out


# pipeline
@dataclass
class Built:
    work: Path
    run_id: str
    revision: int
    started_at: str
    seed: Path
    decision: gate.GateDecision | None = None
    history_in_sha: str | None = None

    @property
    def mart_path(self) -> Path:
        return self.work / "mart.duckdb"

    @property
    def history_path(self) -> Path:
        return self.work / "history" / "current.duckdb"

    @property
    def previous_path(self) -> Path:
        return self.work / "history" / "previous.duckdb"

    def mart(self):
        return duckdb.connect(str(self.mart_path), read_only=True)

    def history(self):
        return duckdb.connect(str(self.history_path), read_only=True)

    def q(self, sql: str, *params, db: str = "mart") -> list[tuple]:
        con = self.mart() if db == "mart" else self.history()
        try:
            return con.execute(sql, list(params)).fetchall()
        finally:
            con.close()

    def run_results(self, command: str = "build") -> dict:
        return json.loads((self.work / "target" / f"run_results.{command}.json").read_text())

    def vars(self) -> dict:
        return self.run_results()["args"]["vars"]

    def failing(self) -> list[str]:
        return [r["unique_id"] for r in self.run_results()["results"] if r["status"] in ("fail", "error")]

    def changes(self, revision: int | None = None) -> dict[tuple[str, str], str]:
        rev = self.revision if revision is None else revision
        return {(t, k): op for t, k, op in self.q(
            "SELECT entity_type, entity_key, operation FROM dataset_changes WHERE revision = ?", rev, db="history")}


def add_release(history: Path, built: Built, *, level: str = "pass") -> None:
    con = duckdb.connect(str(history))
    try:
        con.execute(RELEASE_DDL)
        con.execute("INSERT INTO release VALUES (?, ?, '1', ?, 's', '{}', '{}', 'm', 'sig', ?, 1, FALSE, ?, ?)",
                    [built.revision, built.run_id, "0" * 64, built.started_at, level, built.run_id])
    finally:
        con.close()


def build(seed: Path, work: Path, *, revision: int = 1, bootstrap: bool | None = None,
          started_at: str = "2026-10-05T00:00:00Z", history_from: Path | None = None,
          mutate_records: Callable[[R.Records], R.Records] | None = None, scope=None,
          project_dir: Path | None = None, select: tuple[str, ...] = (), policy_path: Path | None = None,
          canary_failed: bool | None = False, decide: bool = True, allow_partial: bool = False) -> Built:
    if bootstrap is None:
        bootstrap = history_from is None
    work.mkdir(parents=True, exist_ok=True)
    run_id = load.new_run_id(started_at)
    records = None
    if mutate_records is not None:
        records = mutate_records(load.parse_seed(seed, run_id=run_id))
    load.load_seed(seed, work, run_id=run_id, started_at=started_at, records=records, allow_partial=allow_partial)
    sha = None
    if history_from is not None:
        (work / "history").mkdir(exist_ok=True)
        shutil.copyfile(history_from, work / "history" / "current.duckdb")
        sha = sha256_file(history_from)
    kwargs = {}
    if project_dir is not None:
        kwargs["project_dir"] = project_dir
    if policy_path is not None:
        kwargs["policy_path"] = policy_path
    wrapper.run_dbt(work, revision=revision, scope=scope or mini_scope(), bootstrap=bootstrap,
                    code_commit=CODE_COMMIT, catalog_commit="c" * 40, policy_commit="p" * 40,
                    select=select, **kwargs)
    built = Built(work, json.loads((work / "transform_run.json").read_text())["run_id"], revision, started_at,
                  seed, history_in_sha=sha)
    if decide:
        built.decision = gate.decide(work, policy_path or gate.POLICY_PATH, canary_failed=canary_failed)
    return built
