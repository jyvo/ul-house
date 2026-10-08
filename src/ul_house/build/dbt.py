"""run dbt transform in a child process"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ul_house.build import gate
from ul_house.settings import Scope

ANALYTICS_DIR = Path(__file__).resolve().parents[3] / "analytics"
HISTORY_SCHEMA = ANALYTICS_DIR / "history_schema.sql"
POLICY_PATH = ANALYTICS_DIR / "gate_policy.toml"

TRANSFORM_RUN_JSON = "transform_run.json"
LOG_TAIL_LINES = 50
COMPILED_ENV = "UL_HOUSE_DBT_COMPILED"

CHILD = (
    "import os, sys\n"
    "from dbt.cli.main import dbtRunner\n"
    "r = dbtRunner().invoke(sys.argv[1:])\n"
    f"out = os.environ.get({COMPILED_ENV!r})\n"
    "if out and r.success:\n"
    "    try:\n"
    "        open(out, 'w', encoding='utf-8').write(r.result.results[0].node.compiled_code)\n"
    "    except Exception as e:\n"
    "        print('no compiled code:', e)\n"
    "sys.exit(0 if r.success else (2 if r.exception else 1))\n"
)


class TransformError(Exception):
    """a broken precondition of the transform (no load, no history, revision not after the previous release). dbt failures are not TransformErrors"""


@dataclass(frozen=True)
class DbtInvocation:
    args: tuple[str, ...]
    exit_code: int
    success: bool
    run_results: Path | None
    log_tail: str                   # last 50 lines from child output

@dataclass(frozen=True)
class TransformRun:
    run_id: str
    revision: int
    bootstrap: bool
    started_at: str
    ended_at: str
    code_commit: str
    parser_version: int
    catalog_version: str
    catalog_commit: str
    policy_version: str
    policy_commit: str
    dirty: bool
    crawl_run_id: int
    seed_sha256: str
    dbt_invocation_id: str | None
    dbt_manifest_sha256: str | None
    dbt_success: bool
    prev_release_revision: int | None
    prev_release_run_id: str | None
    transform_version: int | None = None

    def to_mapping(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_mapping(cls, m: Mapping) -> TransformRun:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in m.items() if k in names})


# registry + vars
def registry_mapping() -> list[dict]:
    from ul_house.database import entities
    return [dataclasses.asdict(e) for e in entities.ENTITIES]


def transform_version(project_dir: Path = ANALYTICS_DIR) -> int:
    """transform_version var of dbt_project.yml"""
    import yaml
    data = yaml.safe_load((Path(project_dir) / "dbt_project.yml").read_text(encoding="utf-8"))
    return int(data["vars"]["transform_version"])


def build_vars(*, revision: int, run_id: str, started_at: str, bootstrap: bool, code_commit: str,
               parser_version: int, catalog_version: str, catalog_commit: str, policy_version: str,
               policy_commit: str, crawl_run_id: int, prev_release_revision: int | None,
               prev_release_run_id: str | None, prev_parser_version: int | None,
               prev_transform_version: int | None = None,
               registry: Sequence[Mapping] | None = None) -> dict:
    return {
        "revision": int(revision),
        "run_id": run_id,
        "started_at": started_at,
        "bootstrap": bool(bootstrap),
        "code_commit": code_commit,
        "parser_version": int(parser_version),
        "catalog_version": catalog_version,
        "catalog_commit": catalog_commit,
        "policy_version": policy_version,
        "policy_commit": policy_commit,
        "crawl_run_id": int(crawl_run_id),
        "prev_release_revision": prev_release_revision,
        "prev_release_run_id": prev_release_run_id,
        "prev_parser_version": prev_parser_version,
        "prev_transform_version": prev_transform_version,
        "aggregates": list(registry) if registry is not None else registry_mapping(),
    }


# child processes
def history_paths(work_dir: Path) -> tuple[Path, Path]:
    """(previous, current) under work/history"""
    history = Path(work_dir) / "history"
    return history / "previous.duckdb", history / "current.duckdb"


def child_env(work_dir: Path, project_dir: Path = ANALYTICS_DIR, env: Mapping[str, str] | None = None) -> dict[str, str]:
    previous, current = history_paths(work_dir)
    out = dict(os.environ)
    out.update({
        "UL_HOUSE_DUCKDB": str(Path(work_dir) / "mart.duckdb"),
        "UL_HOUSE_HISTORY": str(current),
        "UL_HOUSE_HISTORY_PREV": str(previous),
        "DBT_PROFILES_DIR": str(project_dir),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "DO_NOT_TRACK": "1",
    })
    if env:
        out.update(env)
    out["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"
    out["DO_NOT_TRACK"] = "1"
    return out


def invoke(args: Sequence[str], *, work_dir: Path, project_dir: Path = ANALYTICS_DIR,
           env: Mapping[str, str] | None = None) -> DbtInvocation:
    """dbtRunner().invoke(args)"""
    work_dir = Path(work_dir).resolve()
    project_dir = Path(project_dir).resolve()
    target = work_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    (work_dir / "logs").mkdir(parents=True, exist_ok=True)
    (work_dir / "history").mkdir(parents=True, exist_ok=True)
    command = args[0] if args else "dbt"
    results = target / "run_results.json"
    copied = target / f"run_results.{command}.json"
    for stale in (results, copied):
        stale.unlink(missing_ok=True)
    full = [*args, "--project-dir", str(project_dir), "--profiles-dir", str(project_dir),
            "--target-path", str(target), "--log-path", str(work_dir / "logs")]
    proc = subprocess.run([sys.executable, "-c", CHILD, *full], cwd=work_dir,
                          env=child_env(work_dir, project_dir, env),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    run_results = None
    if results.exists():
        shutil.copyfile(results, copied)
        run_results = copied
    tail = "\n".join(proc.stdout.splitlines()[-LOG_TAIL_LINES:])
    return DbtInvocation(args=tuple(full), exit_code=proc.returncode, success=proc.returncode == 0,
                         run_results=run_results, log_tail=tail)


def _ensure_history(work_dir: Path) -> None:
    """empty history files from history_schema.sql when absent (compile / unit tests only)"""
    for path in history_paths(work_dir):
        if not path.exists():
            _apply_history_schema(path)


def compile_inline(sql: str, *, work_dir: Path, vars: Mapping, project_dir: Path = ANALYTICS_DIR) -> str:
    """dbt compile --inline sql > compiled SQL with macros rendered"""
    work_dir = Path(work_dir).resolve()
    _ensure_history(work_dir)
    out = work_dir / "target" / "inline_compiled.sql"
    out.unlink(missing_ok=True)
    inv = invoke(["compile", "--inline", sql, "--vars", json.dumps(dict(vars))], work_dir=work_dir,
                 project_dir=project_dir, env={COMPILED_ENV: str(out)})
    if not inv.success or not out.exists():
        raise TransformError(f"dbt compile --inline failed (exit {inv.exit_code}):\n{inv.log_tail}")
    return out.read_text(encoding="utf-8")


def run_unit_tests(work_dir: Path, *, vars: Mapping, project_dir: Path = ANALYTICS_DIR) -> DbtInvocation:
    """dbt unit tests (never run by run_dbt in production) req work dir"""
    work_dir = Path(work_dir).resolve()
    _ensure_history(work_dir)
    return invoke(["test", "--select", "test_type:unit", "--vars", json.dumps(dict(vars))],
                  work_dir=work_dir, project_dir=project_dir)


# history
def _history_statements() -> list[str]:
    text = HISTORY_SCHEMA.read_text(encoding="utf-8")
    lines = [line.split("--", 1)[0] for line in text.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def _history_tables() -> set[str]:
    import re
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", HISTORY_SCHEMA.read_text(encoding="utf-8")))


def _apply_history_schema(path: Path) -> None:
    import duckdb
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        for statement in _history_statements():
            con.execute(statement)
    finally:
        con.close()


def _missing_history_tables(path: Path) -> set[str]:
    import duckdb
    con = duckdb.connect(str(path), read_only=True)
    try:
        have = {t for (t,) in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()}
    finally:
        con.close()
    return _history_tables() - have


def _ensure_schema(path: Path) -> None:
    """additive only; a complete file is never opened for writing (its bytes stay unchanged)"""
    if not path.exists() or _missing_history_tables(path):
        _apply_history_schema(path)


def prepare_history(work_dir: Path, *, bootstrap: bool) -> tuple[Path, Path]:
    """(previous, current) bootstrap"""
    previous, current = history_paths(work_dir)
    previous.parent.mkdir(parents=True, exist_ok=True)
    if previous.exists():
        current.unlink(missing_ok=True)
    elif bootstrap:
        if current.exists():
            raise TransformError(f"bootstrap, but a history exists at {current}")
        _apply_history_schema(previous)
    elif current.exists():
        current.rename(previous)
    else:
        raise TransformError(f"no history at {current}: pull the published history first (or bootstrap=True for the first release)")
    for wal in (current.with_name(current.name + ".wal"),):
        wal.unlink(missing_ok=True)
    _ensure_schema(previous)
    shutil.copyfile(previous, current)
    _ensure_schema(current)
    return previous, current


def previous_release(previous: Path) -> tuple[int | None, str | None, int | None, int | None]:
    """(revision, transform_run_id, parser_version, transform_version) of the latest release in history"""
    import duckdb
    con = duckdb.connect(str(previous), read_only=True)
    try:
        tables = {t for (t,) in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()}
        if "release" not in tables:
            return None, None, None, None
        row = con.execute("SELECT revision, transform_run_id FROM release "
                          "ORDER BY revision DESC LIMIT 1").fetchone()
        if row is None:
            return None, None, None, None
        revision, run_id = int(row[0]), row[1]
        parser_version = version = None
        if run_id is not None and "transform_run" in tables:
            found = con.execute("SELECT parser_version, transform_version FROM transform_run "
                                "WHERE run_id = ?", [run_id]).fetchone()
            if found is not None:
                parser_version = None if found[0] is None else int(found[0])
                version = None if found[1] is None else int(found[1])
        return revision, run_id, parser_version, version
    finally:
        con.close()


# run
def _read_load_report(work_dir: Path) -> dict:
    path = Path(work_dir) / TRANSFORM_RUN_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TransformError(f"{path} missing or unreadable: run load_seed first ({e})") from e
    for key in ("run_id", "started_at", "seed_sha256", "crawl_run_id", "parser_version"):
        if key not in data:
            raise TransformError(f"{path} lacks {key!r}: run load_seed first")
    return data


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _insert_transform_run(current: Path, run: TransformRun) -> None:
    import duckdb
    m = run.to_mapping()
    columns = ["run_id", "code_commit", "parser_version", "catalog_version", "catalog_commit",
               "policy_version", "dbt_invocation_id", "dbt_manifest_sha256", "started_at", "ended_at",
               "revision", "bootstrap", "policy_commit", "dirty", "crawl_run_id", "seed_sha256",
               "dbt_success", "prev_release_revision", "prev_release_run_id", "transform_version"]
    con = duckdb.connect(str(current))
    try:
        con.execute(f"INSERT OR REPLACE INTO transform_run ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})", [m[c] for c in columns])
    finally:
        con.close()


def run_dbt(work_dir: Path, *, revision: int, scope: Scope, policy_path: Path = POLICY_PATH,
            bootstrap: bool = False, code_commit: str = "unknown", catalog_commit: str = "unknown",
            policy_commit: str = "unknown", dirty: bool = False,
            project_dir: Path = ANALYTICS_DIR, select: tuple[str, ...] = (),
            registry: Sequence[Mapping] | None = None,
            clock: Callable[[], str] = gate.utc_now) -> str:
    """writes work/mart.duckdb (main), work/history/{previous,current}.duckdb, work/target/ and work/transform_run.json"""
    work_dir = Path(work_dir).resolve()
    load = _read_load_report(work_dir)
    policy = gate.load_policy(policy_path)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise TransformError(f"revision must be an int >= 1, not {revision!r}")

    previous, current = prepare_history(work_dir, bootstrap=bootstrap)
    prev_revision, prev_run_id, prev_parser, prev_version = previous_release(previous)
    if prev_revision is not None and revision <= prev_revision:
        raise TransformError(f"revision {revision} is not after the previous release {prev_revision}")

    version = transform_version(project_dir)
    run_vars = build_vars(
        revision=revision, run_id=load["run_id"], started_at=load["started_at"], bootstrap=bootstrap,
        code_commit=code_commit, parser_version=int(load["parser_version"]),
        catalog_version=scope.fingerprint(), catalog_commit=catalog_commit,
        policy_version=policy.fingerprint(), policy_commit=policy_commit,
        crawl_run_id=int(load["crawl_run_id"]), prev_release_revision=prev_revision,
        prev_release_run_id=prev_run_id, prev_parser_version=prev_parser,
        prev_transform_version=prev_version, registry=registry,
    )
    args = ["build", "--exclude-resource-type", "unit_test", "--vars", json.dumps(run_vars)]
    if select:
        args += ["--select", *select]
    inv = invoke(args, work_dir=work_dir, project_dir=project_dir)

    invocation_id = None
    if inv.run_results is not None:
        try:
            invocation_id = json.loads(inv.run_results.read_text(encoding="utf-8"))["metadata"]["invocation_id"]
        except (OSError, ValueError, KeyError, TypeError):
            invocation_id = None
    run = TransformRun(
        run_id=load["run_id"], revision=revision, bootstrap=bootstrap, started_at=load["started_at"],
        ended_at=clock(), code_commit=code_commit, parser_version=int(load["parser_version"]),
        catalog_version=scope.fingerprint(), catalog_commit=catalog_commit,
        policy_version=policy.fingerprint(), policy_commit=policy_commit, dirty=dirty,
        crawl_run_id=int(load["crawl_run_id"]), seed_sha256=load["seed_sha256"],
        dbt_invocation_id=invocation_id,
        dbt_manifest_sha256=_sha256_file(work_dir / "target" / "manifest.json"),
        dbt_success=inv.success, prev_release_revision=prev_revision,
        prev_release_run_id=prev_run_id, transform_version=version,
    )
    _insert_transform_run(current, run)
    (work_dir / TRANSFORM_RUN_JSON).write_text(
        json.dumps(run.to_mapping(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (work_dir / "target" / "dbt_build.log").write_text(inv.log_tail + "\n", encoding="utf-8")
    return run.run_id
