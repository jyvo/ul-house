"""release gate"""
from __future__ import annotations

import hashlib
import json
import math
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

ANALYTICS_DIR = Path(__file__).resolve().parents[3] / "analytics"
POLICY_PATH = ANALYTICS_DIR / "gate_policy.toml"

LEVELS: tuple[str, ...] = ("info", "warning", "blocker", "error")
POLICY_LEVELS: tuple[str, ...] = ("info", "warning", "blocker")
ESCALATION_LEVELS: tuple[str, ...] = ("warning", "blocker")
ENTRY_SCOPES: tuple[str, ...] = ("catalog", "reference", "new_item")
KNOWN_CHECKS: tuple[str, ...] = (
    "optional_field_null", "retired", "count_drop", "parse_failure", "unexplained_churn",
    "icon_missing", "unlisted_release", "wiki_contradiction", "unknown_vocabulary", "canary",
)
GATE_PREFIX = "gate__"
INFO_PREFIX = "info__"
SAMPLE_SIZE = 20
RUN_RESULTS_NAME = "run_results.build.json"

GATE_COLUMNS: tuple[str, ...] = (
    "entity_type", "entity_key", "entry_kind", "is_new_item", "affected", "runs", "detail",
)


def utc_now() -> str:
    """ISO-8601 UTC + Z suffix, whole seconds"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rank(level: str) -> int:
    return LEVELS.index(level)


class PolicyError(ValueError):
    """gate_policy.toml is missing, unreadable or invalid; the message names the offending key"""


class GateError(RuntimeError):
    """a broken precondition of the gate itself (no transform run, decision already written)"""


# policy
@dataclass(frozen=True)
class Escalation:
    scope: str
    level: str
    min_count: int | None = None
    min_pct: float | None = None
    min_runs: int | None = None

    def to_mapping(self) -> dict:
        out: dict[str, Any] = {"scope": self.scope, "level": self.level}
        for name in ("min_count", "min_pct", "min_runs"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


@dataclass(frozen=True)
class Check:
    name: str
    scopes: tuple[tuple[str, str], ...]             # sorted (scope, level)
    escalate: Escalation | None

    def level_for(self, scope: str) -> str | None:
        for name, level in self.scopes:
            if name == scope:
                return level
        return None

    def to_mapping(self) -> dict:
        out: dict[str, Any] = {"scopes": dict(self.scopes)}
        if self.escalate is not None:
            out["escalate"] = self.escalate.to_mapping()
        return out


def _int_at_least_one(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{where} must be an integer, not {value!r}")
    if value < 1:
        raise PolicyError(f"{where} must be >= 1, not {value!r}")
    return value


def _pct(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"{where} must be a number, not {value!r}")
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 100:
        raise PolicyError(f"{where} must be in (0, 100], not {value!r}")
    return value


def _parse_escalation(name: str, data: Any, scopes: dict[str, str]) -> Escalation:
    where = f"check.{name}.escalate"
    if not isinstance(data, Mapping):
        raise PolicyError(f"{where} must be a table")
    allowed = {"scope", "level", "min_count", "min_pct", "min_runs"}
    for key in data:
        if key not in allowed:
            raise PolicyError(f"{where}.{key}: unknown key")
    scope = data.get("scope")
    if scope not in scopes:
        raise PolicyError(f"{where}.scope: {scope!r} is not one of the check's scopes {sorted(scopes)}")
    level = data.get("level")
    if level not in ESCALATION_LEVELS:
        raise PolicyError(f"{where}.level: {level!r} is not one of {ESCALATION_LEVELS}")
    if _rank(level) <= _rank(scopes[scope]):
        raise PolicyError(f"{where}.level: {level!r} is not above the base level {scopes[scope]!r}")
    has_count, has_pct, has_runs = ("min_count" in data), ("min_pct" in data), ("min_runs" in data)
    if has_runs:
        if has_count or has_pct:
            raise PolicyError(f"{where}: min_runs cannot be combined with min_count / min_pct")
        return Escalation(scope, level, min_runs=_int_at_least_one(data["min_runs"], f"{where}.min_runs"))
    if not (has_count and has_pct):
        missing = "min_pct" if has_count else "min_count"
        raise PolicyError(f"{where}.{missing}: min_count and min_pct go together (or min_runs alone)")
    return Escalation(scope, level,
                      min_count=_int_at_least_one(data["min_count"], f"{where}.min_count"),
                      min_pct=_pct(data["min_pct"], f"{where}.min_pct"))


def _parse_check(name: str, data: Any) -> Check:
    where = f"check.{name}"
    if not isinstance(data, Mapping):
        raise PolicyError(f"{where} must be a table")
    for key in data:
        if key not in ("scopes", "escalate"):
            raise PolicyError(f"{where}.{key}: unknown key")
    raw_scopes = data.get("scopes")
    if not isinstance(raw_scopes, Mapping) or not raw_scopes:
        raise PolicyError(f"{where}.scopes must be a non-empty table")
    scopes: dict[str, str] = {}
    for scope, level in raw_scopes.items():
        if scope == "required":
            raise PolicyError(f"{where}.scopes.required: required fields have no policy entry (null is always an error)")
        if scope == "optional":
            raise PolicyError(f"{where}.scopes.optional: field classes are checks (optional_field_null), not scopes")
        if scope not in ENTRY_SCOPES:
            raise PolicyError(f"{where}.scopes.{scope}: unknown scope (one of {ENTRY_SCOPES})")
        if level not in POLICY_LEVELS:
            raise PolicyError(f"{where}.scopes.{scope}: level {level!r} is not one of {POLICY_LEVELS}")
        scopes[scope] = level
    escalate = None
    if "escalate" in data:
        escalate = _parse_escalation(name, data["escalate"], scopes)
    return Check(name, tuple(sorted(scopes.items())), escalate)


@dataclass(frozen=True)
class GatePolicy:
    checks: tuple[Check, ...]               # sorted by name

    @classmethod
    def from_mapping(cls, data: Any) -> GatePolicy:
        if not isinstance(data, Mapping):
            raise PolicyError("policy must be a table")
        for key in data:
            if key != "check":
                raise PolicyError(f"{key}: unknown top-level key (only [check.*])")
        checks = data.get("check")
        if not isinstance(checks, Mapping):
            raise PolicyError("check: missing [check.*] tables")
        for name in checks:
            if name not in KNOWN_CHECKS:
                raise PolicyError(f"check.{name}: unknown check (one of {KNOWN_CHECKS})")
        for name in KNOWN_CHECKS:
            if name not in checks:
                raise PolicyError(f"check.{name}: missing (every known check needs a policy)")
        return cls(tuple(_parse_check(name, checks[name]) for name in sorted(checks)))

    def to_mapping(self) -> dict:
        return {"check": {c.name: c.to_mapping() for c in self.checks}}

    def stamp(self) -> str:
        return json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        """sha256 hex of stamp() -> release's policy_version"""
        return hashlib.sha256(self.stamp().encode("utf-8")).hexdigest()

    def check(self, name: str) -> Check:
        for c in self.checks:
            if c.name == name:
                return c
        raise KeyError(name)


def load_policy(path: Path | str = POLICY_PATH) -> GatePolicy:
    """gate_policy.toml -> GatePolicy (PolicyError: when missing, not TOML, or invalid)"""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PolicyError(f"cannot read policy {path}: {e}") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PolicyError(f"{path}: not valid TOML: {e}") from e
    return GatePolicy.from_mapping(data)


# eval inputs
@dataclass(frozen=True)
class NodeResult:
    """one entry of dbt's run_results.json"""
    unique_id: str
    status: str
    failures: int | None = None
    message: str | None = None
    relation_name: str | None = None

    @property
    def resource_type(self) -> str:
        return self.unique_id.split(".", 1)[0]

    @property
    def name(self) -> str:
        parts = self.unique_id.split(".")
        return parts[2] if len(parts) > 2 else self.unique_id

    @classmethod
    def from_mapping(cls, m: Mapping) -> NodeResult:
        return cls(unique_id=str(m["unique_id"]), status=str(m.get("status")),
                   failures=m.get("failures"), message=m.get("message"),
                   relation_name=m.get("relation_name"))


@dataclass(frozen=True)
class GateRow:
    """one stored row of a gate test (gate_row macro's columns)"""
    entity_type: str
    entity_key: str
    entry_kind: str | None
    is_new_item: bool = False
    affected: int = 1
    runs: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class PastFinding:
    """obs_gate_finding row, with that run's start time"""
    run_id: str
    started_at: str
    check: str
    scope: str
    level: str


@dataclass(frozen=True)
class RunContext:
    run_id: str
    revision: int
    previous_revision: int | None
    policy_version: str
    policy_commit: str
    dbt_invocation_id: str | None
    decided_at: str
    started_at: str = ""
    dbt_success: bool = True
    problems: tuple[str, ...] = ()


# decision
@dataclass(frozen=True)
class Finding:
    check: str
    level: str
    scope: str
    count: int
    population: int | None
    pct: float | None
    escalated: bool
    consecutive_runs: int               # current run included
    sources: tuple[str, ...] = ()
    sample: tuple[str, ...] = ()
    message: str = ""

    def to_mapping(self) -> dict:
        return {
            "check": self.check, "level": self.level, "scope": self.scope, "count": self.count,
            "population": self.population, "pct": self.pct, "escalated": self.escalated,
            "consecutive_runs": self.consecutive_runs, "sources": list(self.sources),
            "sample": list(self.sample), "message": self.message,
        }

    @classmethod
    def from_mapping(cls, m: Mapping) -> Finding:
        return cls(check=m["check"], level=m["level"], scope=m["scope"], count=int(m["count"]),
                   population=m.get("population"), pct=m.get("pct"), escalated=bool(m["escalated"]),
                   consecutive_runs=int(m["consecutive_runs"]), sources=tuple(m.get("sources", ())),
                   sample=tuple(m.get("sample", ())), message=m.get("message", ""))


def _finding_order(f: Finding) -> tuple:
    return (-_rank(f.level), f.check, f.scope, f.escalated, f.sources, f.message)


@dataclass(frozen=True)
class GateDecision:
    level: Literal["pass", "warning", "blocker", "error"]
    run_id: str
    revision: int
    previous_revision: int | None
    policy_version: str
    policy_commit: str
    dbt_invocation_id: str | None
    canary: Literal["ok", "failed", "not_run"]
    decided_at: str
    findings: tuple[Finding, ...] = field(default=())       # sorted (level desc, check, scope)

    @property
    def publishable(self) -> bool:
        return self.level in ("pass", "warning")

    @property
    def held(self) -> bool:
        return self.level == "blocker"

    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level in ("warning", "blocker"))

    def to_mapping(self) -> dict:
        return {
            "level": self.level, "run_id": self.run_id, "revision": self.revision,
            "previous_revision": self.previous_revision, "policy_version": self.policy_version,
            "policy_commit": self.policy_commit, "dbt_invocation_id": self.dbt_invocation_id,
            "canary": self.canary, "decided_at": self.decided_at,
            "findings": [f.to_mapping() for f in self.findings],
        }

    @classmethod
    def from_mapping(cls, m: Mapping) -> GateDecision:
        return cls(level=m["level"], run_id=m["run_id"], revision=int(m["revision"]),
                   previous_revision=m.get("previous_revision"), policy_version=m["policy_version"],
                   policy_commit=m["policy_commit"], dbt_invocation_id=m.get("dbt_invocation_id"),
                   canary=m["canary"], decided_at=m["decided_at"],
                   findings=tuple(Finding.from_mapping(f) for f in m.get("findings", ())))

    def to_json(self) -> str:
        return json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _gate_check(test_name: str) -> str:
    return test_name[len(GATE_PREFIX):].split("__", 1)[0]


def _resolve_scope(check: Check | None, row: GateRow) -> str:
    """new_item if the row is new and the check lists new_item, else its entry_kind"""
    if row.is_new_item and check is not None and check.level_for("new_item") is not None:
        return "new_item"
    return row.entry_kind or "catalog"


def _consecutive(history: Sequence[PastFinding], context: RunContext, check: str, scope: str) -> int:
    runs: dict[str, str] = {}
    hits: set[str] = set()
    for past in history:
        if past.run_id == context.run_id:
            continue
        if context.started_at and past.started_at and past.started_at >= context.started_at:
            continue
        runs.setdefault(past.run_id, past.started_at)
        if past.check == check and past.scope == scope and _rank(past.level) >= _rank("warning"):
            hits.add(past.run_id)
    streak = 0
    for run_id, _ in sorted(runs.items(), key=lambda kv: (kv[1], kv[0]), reverse=True):
        if run_id not in hits:
            break
        streak += 1
    return 1 + streak


def evaluate(policy: GatePolicy, results: Sequence[NodeResult], rows: Mapping[str, Sequence[GateRow]], *, population: Mapping[str, int] | None, 
             history: Sequence[PastFinding], canary_failed: bool | None, context: RunContext) -> GateDecision:
    raw: list[dict] = []

    def add(check: str, level: str, scope: str, count: int, *, pop: int | None = None,
            pct: float | None = None, escalated: bool = False, sources: Sequence[str] = (),
            sample: Sequence[str] = (), message: str = "") -> None:
        raw.append(dict(check=check, level=level, scope=scope, count=count, population=pop, pct=pct,
                        escalated=escalated, sources=tuple(sorted(sources)),
                        sample=tuple(sorted(sample))[:SAMPLE_SIZE], message=message))

    # errors
    for problem in context.problems:
        add("dbt", "error", "all", 1, message=problem)
    if not results and not context.problems:
        add("dbt", "error", "all", 1, message="run_results.json lists no results")
    failing = 0
    policy_results: list[NodeResult] = []
    for r in results:
        name = r.name
        is_policy = name.startswith(GATE_PREFIX)
        is_info = name.startswith(INFO_PREFIX)
        if r.status in ("error", "fail", "skipped", "runtime error"):
            failing += 1
            what = {"skipped": "skipped", "fail": "failed"}.get(r.status, "errored")
            add("dbt", "error", "all", int(r.failures or 1), sources=(r.unique_id,),
                message=f"{r.resource_type} {name} {what}: {r.message or ''}".strip())
        elif r.status == "warn":
            if is_policy:
                policy_results.append(r)
            elif is_info:
                add(name, "info", "all", int(r.failures or 0), sources=(r.unique_id,),
                    message=r.message or f"{name}: {r.failures} row(s)")
            else:
                add(name, "info", "all", int(r.failures or 0), sources=(r.unique_id,),
                    message=f"unclassified warn test {name}")
        elif is_policy and r.status == "pass":
            continue
    if not context.dbt_success and failing == 0:
        add("dbt", "error", "all", 1, message="dbt reported failure but no node failed")

    # policy checks
    grouped: dict[tuple[str, str], dict[tuple[str, str], GateRow]] = {}
    sources: dict[tuple[str, str], set[str]] = {}
    unknown: list[NodeResult] = []
    for r in policy_results:
        check_name = _gate_check(r.name)
        try:
            check = policy.check(check_name)
        except KeyError:
            unknown.append(r)
            continue
        for row in rows.get(r.unique_id, ()):
            scope = _resolve_scope(check, row)
            entities = grouped.setdefault((check_name, scope), {})
            key = (row.entity_type, row.entity_key)
            prior = entities.get(key)
            if prior is None or (row.affected or 0) > (prior.affected or 0) or \
                    (row.runs or 0) > (prior.runs or 0):
                entities[key] = row
            sources.setdefault((check_name, scope), set()).add(r.unique_id)
    for r in unknown:
        add(_gate_check(r.name), "info", "all", int(r.failures or 0), sources=(r.unique_id,),
            message=f"gate test {r.name} names no policy check")

    for (check_name, scope), entities in sorted(grouped.items()):
        check = policy.check(check_name)
        base = check.level_for(scope)
        pop = population.get(scope) if population is not None else None
        srcs = sources[(check_name, scope)]
        if base is None:
            count = sum(max(int(row.affected or 0), 0) for row in entities.values())
            add(check_name, "info", scope, count, pop=pop, sources=srcs,
                sample=[k for _, k in entities], message=f"{check_name}: scope {scope} not in policy")
            continue
        esc = check.escalate if check.escalate is not None and check.escalate.scope == scope else None
        if esc is not None and esc.min_runs is not None:
            high = {k: row for k, row in entities.items() if (row.runs or 0) >= esc.min_runs}
            low = {k: row for k, row in entities.items() if k not in high}
            for part, level, escalated in ((high, esc.level, True), (low, base, False)):
                if not part:
                    continue
                count = sum(max(int(row.affected or 0), 0) for row in part.values())
                runs = max((row.runs or 0) for row in part.values())
                add(check_name, level, scope, count, pop=pop, escalated=escalated, sources=srcs,
                    sample=[k for _, k in part],
                    message=f"{check_name}: {count} in {scope}" + (f", up to {runs} consecutive runs" if runs else ""))
            continue
        count = sum(max(int(row.affected or 0), 0) for row in entities.values())
        if count <= 0:
            continue
        pct = 100.0 * count / pop if pop else None
        level, escalated = base, False
        if esc is not None and pct is not None and count >= (esc.min_count or 0) and pct >= (esc.min_pct or 0):
            level, escalated = esc.level, True
        message = f"{check_name}: {count} in {scope}" + (f" ({pct:.2f}% of {pop})" if pct is not None else "")
        add(check_name, level, scope, count, pop=pop, pct=pct, escalated=escalated, sources=srcs,
            sample=[k for _, k in entities], message=message)

    # canary
    if canary_failed is True:
        level = policy.check("canary").level_for("catalog") or "blocker"
        add("canary", level, "catalog", 1, message="schema-drift canary failed")
        canary = "failed"
    elif canary_failed is None:
        add("canary", "info", "catalog", 0, message="canary not run")
        canary = "not_run"
    else:
        canary = "ok"

    findings = tuple(sorted(
        (Finding(consecutive_runs=_consecutive(history, context, f["check"], f["scope"]), **f) for f in raw),
        key=_finding_order,
    ))
    top = max((_rank(f.level) for f in findings), default=0)
    level = "pass" if top == 0 else LEVELS[top]
    return GateDecision(level=level, run_id=context.run_id, revision=context.revision,
                        previous_revision=context.previous_revision,
                        policy_version=context.policy_version, policy_commit=context.policy_commit,
                        dbt_invocation_id=context.dbt_invocation_id, canary=canary,
                        decided_at=context.decided_at, findings=findings)


# i/o
def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_results(work_dir: Path, revision: int) -> tuple[list[NodeResult], list[str], str | None]:
    path = work_dir / "target" / RUN_RESULTS_NAME
    try:
        data = _read_json(path)
    except (OSError, ValueError) as e:
        return [], [f"run_results missing or unreadable ({path.name}): {e}"], None
    if not isinstance(data, Mapping) or not isinstance(data.get("results"), list):
        return [], [f"run_results malformed ({path.name})"], None
    problems = []
    results = [NodeResult.from_mapping(r) for r in data["results"]]
    if not results:
        problems.append("run_results.json lists no results")
    run_vars = (data.get("args") or {}).get("vars")
    if isinstance(run_vars, str):
        try:
            run_vars = json.loads(run_vars)
        except ValueError:
            run_vars = None
    if not isinstance(run_vars, Mapping) or run_vars.get("revision") != revision:
        got = run_vars.get("revision") if isinstance(run_vars, Mapping) else None
        problems.append(f"run_results is for revision {got!r}, not {revision}")
    invocation = (data.get("metadata") or {}).get("invocation_id")
    return results, problems, invocation


def _relation_parts(relation_name: str) -> list[str]:
    parts, cur, quoted = [], "", False
    for ch in relation_name:
        if ch == '"':
            quoted = not quoted
        elif ch == "." and not quoted:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def _stored_rows(mart: Path, results: Sequence[NodeResult]) -> tuple[dict[str, list[GateRow]], list[str]]:
    wanted = [r for r in results if r.name.startswith(GATE_PREFIX) and r.status == "warn"]
    if not wanted:
        return {}, []
    import duckdb  # build only
    out: dict[str, list[GateRow]] = {}
    problems: list[str] = []
    if not mart.exists():
        return {}, [f"{mart.name} missing: stored failures unreadable"]
    con = duckdb.connect(str(mart), read_only=True)
    try:
        for r in wanted:
            if not r.relation_name:
                problems.append(f"{r.name}: no stored-failure relation in run_results")
                continue
            parts = _relation_parts(r.relation_name)
            schema, table = parts[-2], parts[-1]
            quoted = f'"{schema}"."{table}"'
            try:
                cols = [d[0] for d in con.execute(f"SELECT * FROM {quoted} LIMIT 0").description]
                missing = [c for c in GATE_COLUMNS if c not in cols]
                if missing:
                    problems.append(f"{r.name}: stored rows lack gate columns {missing}")
                    continue
                select = ", ".join(f'"{c}"' for c in GATE_COLUMNS)
                fetched = con.execute(f"SELECT {select} FROM {quoted}").fetchall()
            except duckdb.Error as e:
                problems.append(f"{r.name}: stored rows unreadable: {e}")
                continue
            out[r.unique_id] = [
                GateRow(entity_type=str(et), entity_key=str(ek), entry_kind=kind,
                        is_new_item=bool(new), affected=int(aff if aff is not None else 1),
                        runs=None if runs is None else int(runs), detail=detail)
                for et, ek, kind, new, aff, runs, detail in fetched
            ]
    finally:
        con.close()
    return out, problems


def _history_inputs(history: Path, prev_run_id: str | None, has_previous: bool) -> tuple[dict[str, int] | None, list[PastFinding], list[str]]:
    problems: list[str] = []
    if not history.exists():
        return None, [], [f"history file {history.name} missing"]
    import duckdb
    con = duckdb.connect(str(history), read_only=True)
    try:
        tables = {t for (t,) in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()}
        population: dict[str, int] | None = None
        if has_previous:
            rows = []
            if "population" in tables and prev_run_id is not None:
                rows = con.execute("SELECT scope, value FROM population WHERE run_id = ?",
                                   [prev_run_id]).fetchall()
            if not rows:
                problems.append("history incomplete: no population rows for the previous release "
                                f"(run {prev_run_id!r})")
            else:
                population = {scope: int(value) for scope, value in rows}
        past: list[PastFinding] = []
        if "obs_gate_finding" in tables:
            started: dict[str, str] = {}
            if "obs_gate_decision" in tables:
                started.update({r: o for r, o in con.execute(
                    "SELECT run_id, observed_at FROM obs_gate_decision").fetchall()})
            if "transform_run" in tables:
                started.update({r: s for r, s in con.execute(
                    "SELECT run_id, started_at FROM transform_run").fetchall()})
            for run_id, check, scope, level, observed in con.execute(
                    "SELECT run_id, check_name, scope, level, observed_at FROM obs_gate_finding").fetchall():
                past.append(PastFinding(run_id, started.get(run_id, observed), check, scope, level))
            for run_id, at in started.items():
                past.append(PastFinding(run_id, at, "", "", "info"))
        return population, past, problems
    finally:
        con.close()


def decide(work_dir: Path, policy_path: Path = POLICY_PATH, *, canary_failed: bool | None = None,
           decided_at: str | None = None) -> GateDecision:
    work_dir = Path(work_dir)
    policy = load_policy(policy_path)
    run_path = work_dir / "transform_run.json"
    try:
        run = _read_json(run_path)
    except (OSError, ValueError) as e:
        raise GateError(f"{run_path} missing or unreadable: run run_dbt first ({e})") from e
    if "revision" not in run:
        raise GateError(f"{run_path} has no revision: run run_dbt first")
    revision = int(run["revision"])
    results, problems, invocation = _run_results(work_dir, revision)
    rows, row_problems = _stored_rows(work_dir / "mart.duckdb", results)
    problems += row_problems
    prev_revision = run.get("prev_release_revision")
    population, past, history_problems = _history_inputs(
        work_dir / "history" / "current.duckdb", run.get("prev_release_run_id"), prev_revision is not None)
    problems += history_problems
    context = RunContext(
        run_id=run["run_id"], revision=revision, previous_revision=prev_revision,
        policy_version=policy.fingerprint(), policy_commit=run.get("policy_commit", "unknown"),
        dbt_invocation_id=run.get("dbt_invocation_id") or invocation,
        decided_at=decided_at or utc_now(), started_at=run.get("started_at", ""),
        dbt_success=bool(run.get("dbt_success", True)), problems=tuple(problems),
    )
    return evaluate(policy, results, rows, population=population, history=past,
                    canary_failed=canary_failed, context=context)


def _finding_rows(decision: GateDecision) -> list[tuple]:
    """one obs_gate_finding row per (check, scope): highest level, counts, any escalated"""
    merged: dict[tuple[str, str], dict] = {}
    for f in decision.findings:
        key = (f.check, f.scope)
        m = merged.get(key)
        if m is None:
            merged[key] = dict(level=f.level, count=f.count, population=f.population, pct=f.pct,
                               escalated=f.escalated, consecutive_runs=f.consecutive_runs)
            continue
        if _rank(f.level) > _rank(m["level"]):
            m["level"], m["pct"] = f.level, f.pct
        m["count"] += f.count
        m["escalated"] = m["escalated"] or f.escalated
        m["consecutive_runs"] = max(m["consecutive_runs"], f.consecutive_runs)
    return [(decision.run_id, check, scope, m["level"], m["count"], m["population"], m["pct"],
             m["escalated"], m["consecutive_runs"], decision.decided_at)
            for (check, scope), m in sorted(merged.items())]


def write(decision: GateDecision, work_dir: Path) -> Path:
    import duckdb
    work_dir = Path(work_dir)
    history = work_dir / "history" / "current.duckdb"
    if not history.exists():
        raise GateError(f"{history} missing: run run_dbt first")
    con = duckdb.connect(str(history))
    try:
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(
                "INSERT INTO obs_gate_decision (run_id, revision, level, policy_version, policy_commit, "
                "decision_json, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [decision.run_id, decision.revision, decision.level, decision.policy_version,
                 decision.policy_commit, decision.to_json(), decision.decided_at])
            for row in _finding_rows(decision):
                con.execute(
                    "INSERT INTO obs_gate_finding (run_id, check_name, scope, level, count, population, "
                    "pct, escalated, consecutive_runs, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    list(row))
            con.execute("COMMIT")
        except duckdb.ConstraintException as e:
            con.execute("ROLLBACK")
            raise GateError(f"gate decision for run {decision.run_id} already written: {e}") from e
        except duckdb.Error:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    path = work_dir / "gate.json"
    path.write_text(json.dumps(decision.to_mapping(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
