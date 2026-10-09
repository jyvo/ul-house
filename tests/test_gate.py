"""build/gate.py policy validation and the release gate (no dbt run)"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import tomllib
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from ul_house.build import gate


EXPECTED_POLICY: dict = {
    "check": {
        "optional_field_null": {
            "scopes": {"new_item": "info", "reference": "info", "catalog": "warning"},
            "escalate": {"scope": "catalog", "level": "blocker", "min_count": 25, "min_pct": 5.0},
        },
        "retired": {
            "scopes": {"reference": "warning", "catalog": "warning"},
            "escalate": {"scope": "catalog", "level": "blocker", "min_count": 25, "min_pct": 1.0},
        },
        "count_drop": {
            "scopes": {"catalog": "info"},
            "escalate": {"scope": "catalog", "level": "blocker", "min_count": 1, "min_pct": 20.0},
        },
        "parse_failure": {
            "scopes": {"catalog": "warning"},
            "escalate": {"scope": "catalog", "level": "blocker", "min_count": 5, "min_pct": 1.0},
        },
        "unexplained_churn": {
            "scopes": {"catalog": "info"},
            "escalate": {"scope": "catalog", "level": "blocker", "min_count": 50, "min_pct": 30.0},
        },
        "icon_missing": {
            "scopes": {"reference": "info", "new_item": "warning", "catalog": "warning"},
            "escalate": {"scope": "catalog", "level": "blocker", "min_count": 10, "min_pct": 2.0},
        },
        "unlisted_release": {
            "scopes": {"catalog": "info"},
            "escalate": {"scope": "catalog", "level": "warning", "min_runs": 3},
        },
        "canary": {"scopes": {"catalog": "blocker"}},
        "wiki_contradiction": {
            "scopes": {"reference": "info", "catalog": "warning"},
            "escalate": {"scope": "catalog", "level": "blocker", "min_count": 5, "min_pct": 1.0},
        },
        "unknown_vocabulary": {
            "scopes": {"reference": "info", "catalog": "warning"},
            "escalate": {"scope": "catalog", "level": "blocker", "min_count": 5, "min_pct": 1.0},
        },
    }
}

LEVEL_RANK = {"pass": -1, "info": 0, "warning": 1, "blocker": 2, "error": 3}


def toml_text(data: dict) -> str:
    def value(v):
        if isinstance(v, str):
            return json.dumps(v)
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, float):
            return repr(v)
        return str(v)

    lines = []
    for name, body in data["check"].items():
        lines.append(f"[check.{name}]")
        for key, table in body.items():
            lines.append(f"{key} = {{ " + ", ".join(f"{k} = {value(v)}" for k, v in table.items()) + " }")
        lines.append("")
    return "\n".join(lines)


def policy_mapping() -> dict:
    return copy.deepcopy(EXPECTED_POLICY)


@pytest.fixture
def policy_file(tmp_path) -> Path:
    path = tmp_path / "gate_policy.toml"
    path.write_text(toml_text(EXPECTED_POLICY))
    return path


class TestShippedPolicy:
    def test_file_equals_the_pinned_mapping(self):
        assert tomllib.loads(gate.POLICY_PATH.read_text()) == EXPECTED_POLICY

    def test_load_policy_default_path(self):
        assert gate.load_policy() == gate.GatePolicy.from_mapping(EXPECTED_POLICY)

    def test_known_checks_are_exactly_the_policy_checks(self):
        assert set(gate.KNOWN_CHECKS) == set(EXPECTED_POLICY["check"])
        assert {"wiki_contradiction", "unknown_vocabulary"} <= set(gate.KNOWN_CHECKS)

    def test_levels_and_scopes_vocabulary(self):
        assert gate.LEVELS == ("info", "warning", "blocker", "error")
        assert gate.POLICY_LEVELS == ("info", "warning", "blocker")
        assert set(gate.ENTRY_SCOPES) == {"catalog", "reference", "new_item"}


class TestPolicyObject:
    def test_roundtrip(self):
        policy = gate.GatePolicy.from_mapping(EXPECTED_POLICY)
        assert gate.GatePolicy.from_mapping(policy.to_mapping()) == policy

    def test_checks_sorted_by_name(self):
        policy = gate.GatePolicy.from_mapping(EXPECTED_POLICY)
        names = [c.name for c in policy.checks]
        assert names == sorted(names)

    def test_lookup_and_level_for(self):
        policy = gate.GatePolicy.from_mapping(EXPECTED_POLICY)
        retired = policy.check("retired")
        assert retired.level_for("catalog") == "warning"
        assert retired.level_for("new_item") is None
        assert retired.escalate.min_count == 25
        assert retired.escalate.min_pct == 1.0
        assert policy.check("unlisted_release").escalate.min_runs == 3
        assert policy.check("canary").escalate is None

    def test_stamp_is_canonical_json_and_fingerprint_its_sha256(self):
        policy = gate.GatePolicy.from_mapping(EXPECTED_POLICY)
        assert policy.stamp() == json.dumps(policy.to_mapping(), sort_keys=True, separators=(",", ":"))
        assert policy.fingerprint() == hashlib.sha256(policy.stamp().encode()).hexdigest()

    def test_fingerprint_ignores_comments_whitespace_and_key_order(self, tmp_path):
        base = tmp_path / "a.toml"
        base.write_text(toml_text(EXPECTED_POLICY))
        shuffled = {"check": {}}
        for name in reversed(list(EXPECTED_POLICY["check"])):
            body = EXPECTED_POLICY["check"][name]
            shuffled["check"][name] = {k: dict(reversed(list(v.items()))) for k, v in reversed(list(body.items()))}
        other = tmp_path / "b.toml"
        other.write_text("# a comment\n\n\n" + toml_text(shuffled).replace("{ ", "{   ").replace("\n", "   # trailing\n"))
        assert gate.load_policy(base).fingerprint() == gate.load_policy(other).fingerprint()

    @pytest.mark.parametrize("mutation", [
        lambda d: d["check"]["retired"]["escalate"].update(min_count=26),
        lambda d: d["check"]["retired"]["escalate"].update(min_pct=1.5),
        lambda d: d["check"]["unlisted_release"]["escalate"].update(min_runs=4),
        lambda d: d["check"]["count_drop"]["scopes"].update(catalog="warning"),
        lambda d: d["check"]["canary"]["scopes"].update(catalog="warning"),
        lambda d: d["check"]["icon_missing"]["scopes"].update(reference="warning"),
    ])
    def test_fingerprint_changes_with_any_value(self, mutation):
        changed = policy_mapping()
        mutation(changed)
        assert gate.GatePolicy.from_mapping(changed).fingerprint() != gate.GatePolicy.from_mapping(
            EXPECTED_POLICY).fingerprint()

    def test_frozen(self):
        policy = gate.GatePolicy.from_mapping(EXPECTED_POLICY)
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.checks = ()
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.check("retired").name = "x"
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.check("retired").escalate.min_count = 1


def _mutate(fn):
    data = policy_mapping()
    fn(data)
    return data


REJECTIONS = {
    "unknown_top_key": lambda d: d.update(extra={}),
    "unknown_check_key": lambda d: d["check"]["retired"].update(weight=1),
    "unknown_check": lambda d: d["check"].update(mystery={"scopes": {"catalog": "info"}}),
    "missing_known_check": lambda d: d["check"].pop("icon_missing"),
    "scope_required": lambda d: d["check"]["retired"]["scopes"].update(required="error"),
    "scope_optional": lambda d: d["check"]["retired"]["scopes"].update(optional="info"),
    "unknown_scope": lambda d: d["check"]["retired"]["scopes"].update(everything="info"),
    "level_error_in_scopes": lambda d: d["check"]["retired"]["scopes"].update(reference="error"),
    "unknown_level": lambda d: d["check"]["retired"]["scopes"].update(reference="critical"),
    "escalate_level_not_above_base": lambda d: d["check"]["retired"]["escalate"].update(level="warning"),
    "escalate_level_info": lambda d: d["check"]["retired"]["escalate"].update(level="info"),
    "escalate_level_error": lambda d: d["check"]["retired"]["escalate"].update(level="error"),
    "escalate_scope_not_in_scopes": lambda d: d["check"]["count_drop"]["escalate"].update(scope="reference"),
    "min_count_without_min_pct": lambda d: d["check"]["retired"]["escalate"].pop("min_pct"),
    "min_pct_without_min_count": lambda d: d["check"]["retired"]["escalate"].pop("min_count"),
    "min_runs_with_min_count": lambda d: d["check"]["unlisted_release"]["escalate"].update(min_count=3),
    "min_runs_with_min_pct": lambda d: d["check"]["unlisted_release"]["escalate"].update(min_pct=3.0),
    "escalate_without_any_bound": lambda d: d["check"]["retired"].update(escalate={"scope": "catalog", "level": "blocker"}),
    "min_count_true": lambda d: d["check"]["retired"]["escalate"].update(min_count=True),
    "min_count_zero": lambda d: d["check"]["retired"]["escalate"].update(min_count=0),
    "min_count_negative": lambda d: d["check"]["retired"]["escalate"].update(min_count=-1),
    "min_count_float": lambda d: d["check"]["retired"]["escalate"].update(min_count=2.0),
    "min_pct_nan": lambda d: d["check"]["retired"]["escalate"].update(min_pct=math.nan),
    "min_pct_inf": lambda d: d["check"]["retired"]["escalate"].update(min_pct=math.inf),
    "min_pct_zero": lambda d: d["check"]["retired"]["escalate"].update(min_pct=0),
    "min_pct_over_100": lambda d: d["check"]["retired"]["escalate"].update(min_pct=101),
    "min_pct_bool": lambda d: d["check"]["retired"]["escalate"].update(min_pct=True),
    "min_runs_zero": lambda d: d["check"]["unlisted_release"]["escalate"].update(min_runs=0),
    "min_runs_true": lambda d: d["check"]["unlisted_release"]["escalate"].update(min_runs=True),
    "check_not_a_table": lambda d: d["check"].update(retired=5),
    "check_is_a_list": lambda d: d.update(check=[]),
}


class TestPolicyRejections:
    @pytest.mark.parametrize("name", sorted(REJECTIONS))
    def test_rejected(self, name):
        with pytest.raises(gate.PolicyError):
            gate.GatePolicy.from_mapping(_mutate(REJECTIONS[name]))

    def test_policy_error_is_a_value_error(self):
        assert issubclass(gate.PolicyError, ValueError)

    def test_required_scope_message(self):
        with pytest.raises(gate.PolicyError, match="required"):
            gate.GatePolicy.from_mapping(_mutate(REJECTIONS["scope_required"]))

    def test_optional_scope_message_names_the_check(self):
        with pytest.raises(gate.PolicyError, match="optional_field_null"):
            gate.GatePolicy.from_mapping(_mutate(REJECTIONS["scope_optional"]))

    def test_message_names_the_offending_key(self):
        with pytest.raises(gate.PolicyError, match="min_pct"):
            gate.GatePolicy.from_mapping(_mutate(REJECTIONS["min_pct_over_100"]))

    def test_valid_boundaries_accepted(self):
        data = policy_mapping()
        data["check"]["retired"]["escalate"].update(min_pct=100, min_count=1)
        data["check"]["unlisted_release"]["escalate"]["min_runs"] = 1
        gate.GatePolicy.from_mapping(data)

    def test_missing_file(self, tmp_path):
        with pytest.raises(gate.PolicyError):
            gate.load_policy(tmp_path / "absent.toml")

    def test_toml_syntax_error(self, tmp_path):
        path = tmp_path / "bad.toml"
        path.write_text("[check.retired\nscopes = {")
        with pytest.raises(gate.PolicyError):
            gate.load_policy(path)

    def test_load_policy_validates(self, tmp_path):
        path = tmp_path / "p.toml"
        path.write_text(toml_text(_mutate(REJECTIONS["min_count_zero"])))
        with pytest.raises(gate.PolicyError):
            gate.load_policy(path)


HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS transform_run (
  run_id VARCHAR PRIMARY KEY, code_commit VARCHAR NOT NULL, parser_version BIGINT NOT NULL,
  catalog_version VARCHAR NOT NULL, catalog_commit VARCHAR NOT NULL, policy_version VARCHAR NOT NULL,
  dbt_invocation_id VARCHAR, dbt_manifest_sha256 VARCHAR, started_at VARCHAR NOT NULL,
  ended_at VARCHAR NOT NULL,
  revision BIGINT NOT NULL, bootstrap BOOLEAN NOT NULL, policy_commit VARCHAR NOT NULL,
  dirty BOOLEAN NOT NULL, crawl_run_id BIGINT NOT NULL, seed_sha256 VARCHAR NOT NULL,
  dbt_success BOOLEAN NOT NULL, prev_release_revision BIGINT, prev_release_run_id VARCHAR);
CREATE TABLE IF NOT EXISTS population (
  run_id VARCHAR NOT NULL, revision BIGINT NOT NULL, scope VARCHAR NOT NULL,
  value BIGINT NOT NULL, observed_at VARCHAR NOT NULL, PRIMARY KEY (run_id, scope));
CREATE TABLE IF NOT EXISTS obs_gate_decision (
  run_id VARCHAR PRIMARY KEY, revision BIGINT NOT NULL, level VARCHAR NOT NULL,
  policy_version VARCHAR NOT NULL, policy_commit VARCHAR NOT NULL, decision_json VARCHAR NOT NULL,
  observed_at VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS obs_gate_finding (
  run_id VARCHAR NOT NULL, check_name VARCHAR NOT NULL, scope VARCHAR NOT NULL,
  level VARCHAR NOT NULL, count BIGINT NOT NULL, population BIGINT, pct DOUBLE,
  escalated BOOLEAN NOT NULL, consecutive_runs BIGINT NOT NULL, observed_at VARCHAR NOT NULL,
  PRIMARY KEY (run_id, check_name, scope));
"""

CURRENT_RUN = "t20261005T000000Z-aaaaaa"
PREV_RUN = "t20261004T000000Z-bbbbbb"
CURRENT_AT = "2026-10-05T00:00:00Z"
PREV_POPULATION = {"catalog": 1000, "reference": 100, "new_item": 50}


def gate_test(name: str, rows=(), *, status="warn", failures=None, message=""):
    return {"name": name, "status": status, "rows": list(rows), "failures": failures, "message": message}


def row(key, *, kind="catalog", new=False, affected=1, runs=None, entity_type="equipment", detail=""):
    return {"entity_type": entity_type, "entity_key": key, "entry_kind": kind, "is_new_item": new,
            "affected": affected, "runs": runs, "detail": detail}


def rows_of(n, **kw):
    return [row(f"{i:07d}", **kw) for i in range(n)]


def uid_of(name: str, kind: str = "test") -> str:
    return f"{kind}.ul_house.{name}"


class Work:
    """builds work/{transform_run.json, target/run_results.build.json, mart.duckdb, history/current.duckdb}"""

    def __init__(self, tmp_path: Path):
        self.dir = tmp_path / "work"
        (self.dir / "target").mkdir(parents=True)
        (self.dir / "history").mkdir()
        self.tmp = tmp_path

    def history(self):
        return duckdb.connect(str(self.dir / "history" / "current.duckdb"))

    def build(self, *, tests=(), nodes=(), revision=5, bootstrap=False, prev_revision=4,
              population=PREV_POPULATION, past=(), results_revision=None, dbt_success=True,
              run_results=True, empty_results=False, run_id=CURRENT_RUN, invocation_id="inv-1"):
        mart = duckdb.connect(str(self.dir / "mart.duckdb"))
        mart.execute("CREATE SCHEMA IF NOT EXISTS audit")
        results = []
        for t in tests:
            result = {"status": t["status"], "unique_id": uid_of(t["name"]), "message": t["message"],
                      "failures": t["failures"] if t["failures"] is not None else (
                          len(t["rows"]) if t["status"] in ("warn", "fail") else 0),
                      "execution_time": 0.01, "thread_id": "Thread-1", "adapter_response": {}, "timing": []}
            if t["status"] in ("warn", "fail", "pass"):
                relation = f'"mart"."audit"."{t["name"]}"'
                result["relation_name"] = relation
                mart.execute(f'CREATE TABLE audit."{t["name"]}" (entity_type VARCHAR, entity_key VARCHAR, '
                             "entry_kind VARCHAR, is_new_item BOOLEAN, affected BIGINT, runs BIGINT, detail VARCHAR)")
                for r in t["rows"]:
                    mart.execute(f'INSERT INTO audit."{t["name"]}" VALUES (?, ?, ?, ?, ?, ?, ?)',
                                 [r["entity_type"], r["entity_key"], r["entry_kind"], r["is_new_item"],
                                  r["affected"], r["runs"], r["detail"]])
            results.append(result)
        for node in nodes:
            unique_id, status, message = node
            results.append({"status": status, "unique_id": unique_id, "message": message, "failures": None,
                            "execution_time": 0.01, "thread_id": "Thread-1", "adapter_response": {}, "timing": []})
        mart.close()
        if not results:
            results.append({"status": "success", "unique_id": "model.ul_house.mart_item", "message": "OK",
                            "failures": None, "execution_time": 0.01, "thread_id": "Thread-1",
                            "adapter_response": {}, "timing": []})
        if empty_results:
            results = []

        prev_run = PREV_RUN if not bootstrap else None
        prev_rev = prev_revision if not bootstrap else None
        run = {
            "run_id": run_id, "revision": revision, "bootstrap": bootstrap, "started_at": CURRENT_AT,
            "ended_at": "2026-10-05T00:01:00Z", "code_commit": "c" * 40, "parser_version": 1,
            "catalog_version": "f" * 64, "catalog_commit": "d" * 40, "policy_version": "e" * 64,
            "policy_commit": "b" * 40, "dirty": False, "crawl_run_id": 1, "seed_sha256": "a" * 64,
            "dbt_invocation_id": invocation_id, "dbt_manifest_sha256": "9" * 64, "dbt_success": dbt_success,
            "prev_release_revision": prev_rev, "prev_release_run_id": prev_run,
        }
        (self.dir / "transform_run.json").write_text(json.dumps(run))
        if run_results:
            payload = {
                "metadata": {"invocation_id": invocation_id, "dbt_version": "1.12.5"},
                "args": {"which": "build", "vars": {"revision": revision if results_revision is None else results_revision}},
                "results": results,
                "elapsed_time": 1.0,
            }
            (self.dir / "target" / "run_results.build.json").write_text(json.dumps(payload))

        hist = self.history()
        hist.execute(HISTORY_DDL)
        self._transform_run(hist, run_id, CURRENT_AT, revision, prev_rev, prev_run)
        if not bootstrap and population is not None:
            for scope, value in population.items():
                hist.execute("INSERT INTO population VALUES (?, ?, ?, ?, ?)",
                             [PREV_RUN, prev_revision, scope, value, "2026-10-04T00:00:00Z"])
        for run_no, (at, findings) in enumerate(past, 1):
            rid = f"t-past-{run_no}"
            self._transform_run(hist, rid, at, revision - 10 + run_no, None, None)
            for check, scope, level in findings:
                hist.execute("INSERT INTO obs_gate_finding VALUES (?, ?, ?, ?, 1, NULL, NULL, FALSE, 1, ?)",
                             [rid, check, scope, level, at])
        hist.close()
        return self.dir

    @staticmethod
    def _transform_run(hist, run_id, started_at, revision, prev_rev, prev_run):
        hist.execute("INSERT INTO transform_run VALUES (?, 'c', 1, 'v', 'cc', 'p', NULL, NULL, ?, ?, ?, ?, 'pc', "
                     "FALSE, 1, 's', TRUE, ?, ?)",
                     [run_id, started_at, started_at, revision, prev_rev is None, prev_rev, prev_run])


@pytest.fixture
def work(tmp_path) -> Work:
    return Work(tmp_path)


def decide(w: Work, policy_file: Path, *, canary_failed=False, **build):
    directory = w.build(**build)
    return gate.decide(directory, policy_file, canary_failed=canary_failed, decided_at="2026-10-05T00:02:00Z")


def finding(decision, check, scope=None):
    found = [f for f in decision.findings if f.check == check and (scope is None or f.scope == scope)]
    assert found, f"no finding {check}/{scope} in {[(f.check, f.scope, f.level) for f in decision.findings]}"
    return found[0]


def findings_of(decision, check):
    return [f for f in decision.findings if f.check == check]


def _base_cases():
    """(check, scope, base level) for every policy entry that a gate__ test can raise"""
    for check, body in EXPECTED_POLICY["check"].items():
        if check == "canary":
            continue
        for scope, level in body["scopes"].items():
            yield pytest.param(check, scope, level, id=f"{check}-{scope}-{level}")


def _rows_for(check, scope, n=1):
    if check == "count_drop":
        return [row("*", kind=scope, affected=n, entity_type="equipment")]
    kind, new = ("catalog", True) if scope == "new_item" else (scope, False)
    runs = 1 if check == "unlisted_release" else None
    return [row(f"{i:07d}", kind=kind, new=new, runs=runs) for i in range(n)]


class TestMatrix:
    @pytest.mark.parametrize("check, scope, level", list(_base_cases()))
    def test_one_row_gets_the_base_level(self, work, policy_file, check, scope, level):
        d = decide(work, policy_file, tests=[gate_test(f"gate__{check}", _rows_for(check, scope))])
        f = finding(d, check, scope)
        assert f.level == level
        assert f.escalated is False
        assert f.count == 1
        assert uid_of(f"gate__{check}") in f.sources

    @pytest.mark.parametrize("check, kind", [
        ("count_drop", "reference"),
        ("parse_failure", "reference"),
        ("unexplained_churn", "reference"),
        ("unlisted_release", "reference"),
    ])
    def test_unlisted_scope_is_info(self, work, policy_file, check, kind):
        n = 1
        rows = [row("*" if check == "count_drop" else "0000001", kind=kind, affected=n,
                    runs=1 if check == "unlisted_release" else None)]
        d = decide(work, policy_file, tests=[gate_test(f"gate__{check}", rows)])
        assert finding(d, check).level == "info"
        assert d.level == "pass"

    def test_new_item_precedence_only_where_listed(self, work, policy_file):
        d = decide(work, policy_file, tests=[
            gate_test("gate__optional_field_null", [row("1", new=True)]),
            gate_test("gate__retired", [row("2", new=True)]),
        ])
        assert finding(d, "optional_field_null").scope == "new_item"
        assert finding(d, "optional_field_null").level == "info"
        assert finding(d, "retired").scope == "catalog"
        assert finding(d, "retired").level == "warning"
        assert d.level == "warning"

    def test_new_item_does_not_escalate(self, work, policy_file):
        d = decide(work, policy_file, tests=[gate_test("gate__icon_missing", rows_of(50, new=True))])
        f = finding(d, "icon_missing", "new_item")
        assert (f.level, f.escalated) == ("warning", False)

    def test_escalation_only_in_the_escalation_scope(self, work, policy_file):
        d = decide(work, policy_file, tests=[gate_test("gate__retired", rows_of(100, kind="reference"))])
        f = finding(d, "retired", "reference")
        assert (f.level, f.escalated) == ("warning", False)
        assert d.level == "warning"

    ESCALATION = [
        ("optional_field_null", 25, 500, True),         # 5.0 %
        ("optional_field_null", 25, 501, False),        # 4.99 %
        ("optional_field_null", 24, 100, False),        # 24 %
        ("optional_field_null", 50, 1000, True),
        ("retired", 25, 2500, True),                    # 1.0 % exactly
        ("retired", 25, 2501, False),
        ("retired", 24, 100, False),
        ("retired", 30, 1000, True),
        ("parse_failure", 5, 500, True),
        ("parse_failure", 5, 501, False),
        ("parse_failure", 4, 100, False),
        ("unexplained_churn", 50, 100, True),
        ("unexplained_churn", 50, 167, False),          # 29.94 %
        ("unexplained_churn", 49, 100, False),
        ("icon_missing", 10, 500, True),
        ("icon_missing", 10, 501, False),
        ("icon_missing", 9, 100, False),
        ("wiki_contradiction", 5, 500, True),
        ("wiki_contradiction", 5, 501, False),
        ("wiki_contradiction", 4, 100, False),
        ("unknown_vocabulary", 5, 500, True),
        ("unknown_vocabulary", 4, 100, False),
    ]

    @pytest.mark.parametrize("check, count, population, escalates", ESCALATION,
                             ids=[f"{c}-{n}of{p}-{e}" for c, n, p, e in ESCALATION])
    def test_escalation_needs_both_bounds(self, work, policy_file, check, count, population, escalates):
        d = decide(work, policy_file, tests=[gate_test(f"gate__{check}", rows_of(count))],
                   population={**PREV_POPULATION, "catalog": population})
        f = finding(d, check, "catalog")
        base = EXPECTED_POLICY["check"][check]["scopes"]["catalog"]
        assert f.escalated is escalates
        assert f.level == ("blocker" if escalates else base)
        assert f.count == count
        assert f.population == population
        assert f.pct == pytest.approx(100 * count / population)
        assert d.level == ("blocker" if escalates else ("warning" if base == "warning" else "pass"))

    def test_denominator_is_the_previous_releases_population(self, work, policy_file):
        # live catalog shrank
        d = decide(work, policy_file, tests=[gate_test("gate__retired", rows_of(25))],
                   population={"catalog": 100, "reference": 0, "new_item": 0})
        f = finding(d, "retired", "catalog")
        assert (f.population, f.pct) == (100, 25.0)
        assert f.level == "blocker" and f.escalated

    def test_bootstrap_has_no_denominator_and_no_escalation(self, work, policy_file):
        d = decide(work, policy_file, bootstrap=True, tests=[gate_test("gate__retired", rows_of(100))])
        f = finding(d, "retired", "catalog")
        assert (f.level, f.escalated, f.pct, f.population) == ("warning", False, None, None)
        assert d.previous_revision is None
        assert d.level == "warning"

    def test_missing_population_of_a_previous_release_is_an_error(self, work, policy_file):
        d = decide(work, policy_file, population=None, tests=[gate_test("gate__retired", rows_of(30))])
        assert d.level == "error"
        assert any(f.level == "error" for f in d.findings)

    @pytest.mark.parametrize("dropped, level, escalated", [
        (21, "blocker", True),        # 100 -> 79
        (20, "blocker", True),        # 100 -> 80: equality on min_pct
        (19, "info", False),          # 100 -> 81
    ])
    def test_count_drop(self, work, policy_file, dropped, level, escalated):
        d = decide(work, policy_file, population={**PREV_POPULATION, "catalog": 100},
                   tests=[gate_test("gate__count_drop", [row("*", affected=dropped)])])
        f = finding(d, "count_drop", "catalog")
        assert (f.level, f.escalated) == (level, escalated)
        assert f.count == dropped
        assert f.pct == pytest.approx(float(dropped))

    @pytest.mark.parametrize("runs, level, escalated", [(1, "info", False), (2, "info", False), (3, "warning", True), (7, "warning", True)])
    def test_unlisted_release_min_runs(self, work, policy_file, runs, level, escalated):
        d = decide(work, policy_file, tests=[gate_test("gate__unlisted_release", [row("1999999", runs=runs)])])
        f = finding(d, "unlisted_release", "catalog")
        assert (f.level, f.escalated) == (level, escalated)
        assert f.pct is None
        assert d.level == ("warning" if escalated else "pass")

    def test_unlisted_release_mixed_streaks(self, work, policy_file):
        d = decide(work, policy_file, tests=[gate_test(
            "gate__unlisted_release", [row("1", runs=2), row("2", runs=3)])])
        assert d.level == "warning"
        assert any(f.escalated for f in findings_of(d, "unlisted_release"))

    def test_distinct_entities_across_parts_are_counted_once(self, work, policy_file):
        d = decide(work, policy_file, tests=[
            gate_test("gate__optional_field_null__stats", [row("a"), row("b")]),
            gate_test("gate__optional_field_null__proc", [row("b"), row("c")]),
        ])
        f = finding(d, "optional_field_null", "catalog")
        assert f.count == 3
        assert uid_of("gate__optional_field_null__stats") in f.sources
        assert uid_of("gate__optional_field_null__proc") in f.sources

    def test_affected_is_summed(self, work, policy_file):
        d = decide(work, policy_file, tests=[gate_test("gate__count_drop", [row("*", affected=7)])],
                   population={**PREV_POPULATION, "catalog": 1000})
        assert finding(d, "count_drop").count == 7

    def test_sample_is_sorted_and_capped_at_20(self, work, policy_file):
        keys = [f"{i:07d}" for i in range(30)][::-1]
        d = decide(work, policy_file, tests=[gate_test("gate__retired", [row(k) for k in keys])])
        f = finding(d, "retired", "catalog")
        assert f.count == 30
        assert list(f.sample) == sorted(keys)[:20]

    def test_passing_policy_test_raises_nothing(self, work, policy_file):
        d = decide(work, policy_file, tests=[gate_test("gate__retired", [], status="pass")])
        assert d.level == "pass"
        assert not [f for f in d.findings if f.check == "retired"]

    @pytest.mark.parametrize("canary, level, decision_level", [
        (True, "blocker", "blocker"),
        (None, "info", "pass"),
    ])
    def test_canary(self, work, policy_file, canary, level, decision_level):
        d = decide(work, policy_file, canary_failed=canary)
        assert finding(d, "canary", "catalog").level == level
        assert d.level == decision_level
        assert d.canary == ("failed" if canary else "not_run")

    def test_canary_ok_leaves_no_finding(self, work, policy_file):
        d = decide(work, policy_file, canary_failed=False)
        assert not findings_of(d, "canary")
        assert d.canary == "ok"
        assert d.level == "pass"


class TestErrors:
    def test_failed_error_severity_test(self, work, policy_file):
        d = decide(work, policy_file, nodes=[("test.ul_house.not_null_mart_equipment_uid.abc", "fail", "Got 2 results")])
        assert d.level == "error"
        f = [f for f in d.findings if f.level == "error"][0]
        assert f.check == "dbt"
        assert "test.ul_house.not_null_mart_equipment_uid.abc" in f.sources
        assert not d.publishable and not d.held

    def test_errored_test(self, work, policy_file):
        d = decide(work, policy_file, nodes=[("test.ul_house.relationships_x.abc", "error", "boom")])
        assert d.level == "error"

    def test_model_error(self, work, policy_file):
        d = decide(work, policy_file, nodes=[("model.ul_house.mart_stat", "error", "contract violation")])
        assert d.level == "error"
        assert "model.ul_house.mart_stat" in [s for f in d.findings if f.level == "error" for s in f.sources]

    @pytest.mark.parametrize("unique_id", ["seed.ul_house.vocabulary", "snapshot.ul_house.source_lineage"])
    def test_seed_and_snapshot_error(self, work, policy_file, unique_id):
        d = decide(work, policy_file, nodes=[(unique_id, "error", "x")])
        assert d.level == "error"

    def test_skipped_node(self, work, policy_file):
        d = decide(work, policy_file, nodes=[("model.ul_house.mart_proc", "skipped", "")])
        assert d.level == "error"

    def test_erroring_policy_test(self, work, policy_file):
        d = decide(work, policy_file, tests=[gate_test("gate__retired", status="error", message="could not run")])
        assert d.level == "error"
        assert uid_of("gate__retired") in [s for f in d.findings if f.level == "error" for s in f.sources]

    def test_erroring_info_test(self, work, policy_file):
        d = decide(work, policy_file, tests=[gate_test("info__evolution_unresolved_ref", status="error")])
        assert d.level == "error"

    def test_missing_run_results(self, work, policy_file):
        d = decide(work, policy_file, run_results=False)
        assert d.level == "error"

    def test_empty_run_results(self, work, policy_file):
        d = decide(work, policy_file, empty_results=True)
        assert d.level == "error"

    def test_unreadable_run_results(self, work, policy_file):
        directory = work.build()
        (directory / "target" / "run_results.build.json").write_text("{not json")
        d = gate.decide(directory, policy_file, canary_failed=False)
        assert d.level == "error"

    def test_run_results_of_another_revision(self, work, policy_file):
        d = decide(work, policy_file, revision=5, results_revision=4)
        assert d.level == "error"

    def test_dbt_success_false_without_failing_node(self, work, policy_file):
        d = decide(work, policy_file, dbt_success=False)
        assert d.level == "error"

    @pytest.mark.parametrize("policy_level_rows", [rows_of(1), rows_of(100)])
    def test_error_overrides_whatever_the_policy_says(self, work, policy_file, policy_level_rows):
        d = decide(work, policy_file, canary_failed=True,
                   tests=[gate_test("gate__retired", policy_level_rows)],
                   nodes=[("model.ul_house.mart_item", "error", "x")])
        assert d.level == "error"

    def test_info_test_is_info(self, work, policy_file):
        d = decide(work, policy_file, tests=[gate_test("info__evolution_unresolved_ref", rows_of(8))])
        f = finding(d, "info__evolution_unresolved_ref")
        assert f.level == "info" and f.count == 8
        assert d.level == "pass"

    def test_unclassified_warn_is_info(self, work, policy_file):
        d = decide(work, policy_file, tests=[gate_test("some_other_test", rows_of(2))])
        assert d.level == "pass"
        infos = [f for f in d.findings if uid_of("some_other_test") in f.sources]
        assert infos and all(f.level == "info" for f in infos)


class TestOverall:
    def test_only_info_is_pass(self, work, policy_file):
        d = decide(work, policy_file, tests=[gate_test("info__alias_deferred", rows_of(1))])
        assert d.level == "pass"
        assert d.publishable and not d.held

    def test_warning(self, work, policy_file):
        d = decide(work, policy_file, tests=[gate_test("gate__retired", rows_of(1))])
        assert d.level == "warning"
        assert d.publishable and not d.held

    def test_blocker_holds(self, work, policy_file):
        d = decide(work, policy_file, canary_failed=True)
        assert d.level == "blocker"
        assert d.held and not d.publishable

    def test_max_level_wins(self, work, policy_file):
        d = decide(work, policy_file, canary_failed=True,
                   tests=[gate_test("gate__retired", rows_of(1)), gate_test("info__x", rows_of(1))])
        assert d.level == "blocker"

    def test_warnings_view(self, work, policy_file):
        d = decide(work, policy_file, canary_failed=True, tests=[
            gate_test("gate__retired", rows_of(1)), gate_test("info__alias_deferred", rows_of(1))])
        assert {(f.check, f.level) for f in d.warnings()} == {("retired", "warning"), ("canary", "blocker")}

    def test_findings_sorted_level_desc_then_check_scope(self, work, policy_file):
        d = decide(work, policy_file, canary_failed=True, tests=[
            gate_test("gate__retired", rows_of(1)), gate_test("info__zzz", rows_of(1)),
            gate_test("gate__icon_missing", rows_of(1, kind="reference"))])
        keys = [(-LEVEL_RANK[f.level], f.check, f.scope) for f in d.findings]
        assert keys == sorted(keys)

    def test_lineage_fields(self, work, policy_file):
        d = decide(work, policy_file, revision=5, prev_revision=4)
        assert (d.run_id, d.revision, d.previous_revision) == (CURRENT_RUN, 5, 4)
        assert d.policy_version == gate.load_policy(policy_file).fingerprint()
        assert d.policy_commit == "b" * 40
        assert d.dbt_invocation_id == "inv-1"
        assert d.decided_at == "2026-10-05T00:02:00Z"


class TestConsecutiveRuns:
    def run(self, work, policy_file, past):
        return finding(decide(work, policy_file, tests=[gate_test("gate__retired", rows_of(1))], past=past),
                       "retired", "catalog")

    def test_first_run(self, work, policy_file):
        assert self.run(work, policy_file, []).consecutive_runs == 1

    def test_streak_counts_this_run(self, work, policy_file):
        past = [("2026-10-02T00:00:00Z", [("retired", "catalog", "warning")]),
                ("2026-10-03T00:00:00Z", [("retired", "catalog", "blocker")])]
        assert self.run(work, policy_file, past).consecutive_runs == 3

    def test_gap_resets(self, work, policy_file):
        past = [("2026-10-01T00:00:00Z", [("retired", "catalog", "warning")]),
                ("2026-10-02T00:00:00Z", []),
                ("2026-10-03T00:00:00Z", [("retired", "catalog", "warning")])]
        assert self.run(work, policy_file, past).consecutive_runs == 2

    def test_info_in_between_is_a_gap(self, work, policy_file):
        past = [("2026-10-02T00:00:00Z", [("retired", "catalog", "warning")]),
                ("2026-10-03T00:00:00Z", [("retired", "catalog", "info")])]
        assert self.run(work, policy_file, past).consecutive_runs == 1

    def test_other_scope_or_check_does_not_count(self, work, policy_file):
        past = [("2026-10-03T00:00:00Z", [("retired", "reference", "warning"), ("icon_missing", "catalog", "warning")])]
        assert self.run(work, policy_file, past).consecutive_runs == 1

    def test_order_is_by_started_at_not_insertion(self, work, policy_file):
        past = [("2026-10-03T00:00:00Z", [("retired", "catalog", "warning")]),
                ("2026-10-01T00:00:00Z", []),
                ("2026-10-02T00:00:00Z", [("retired", "catalog", "warning")])]
        assert self.run(work, policy_file, past).consecutive_runs == 3


class TestDecisionObject:
    def sample(self, work, policy_file):
        return decide(work, policy_file, canary_failed=True, tests=[
            gate_test("gate__retired", rows_of(30)), gate_test("info__alias_deferred", rows_of(2))])

    def test_to_mapping_has_t3s_keys(self, work, policy_file):
        m = self.sample(work, policy_file).to_mapping()
        assert {"level", "policy_version", "policy_commit", "findings"} <= set(m)
        assert m["level"] == "blocker"
        for f in m["findings"]:
            assert {"check", "scope", "level", "count", "pct", "message"} <= set(f)
        json.dumps(m)

    def test_mapping_roundtrip(self, work, policy_file):
        d = self.sample(work, policy_file)
        assert gate.GateDecision.from_mapping(d.to_mapping()) == d
        assert gate.GateDecision.from_mapping(json.loads(d.to_json())) == d

    def test_to_json_is_canonical(self, work, policy_file):
        d = self.sample(work, policy_file)
        assert d.to_json() == json.dumps(d.to_mapping(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    def test_same_inputs_same_bytes(self, tmp_path, policy_file):
        a = decide(Work(tmp_path / "a"), policy_file, tests=[gate_test("gate__retired", rows_of(30))])
        b = decide(Work(tmp_path / "b"), policy_file, tests=[gate_test("gate__retired", rows_of(30))])
        assert a.to_json() == b.to_json()

    def test_decide_twice_does_not_write(self, work, policy_file):
        directory = work.build(tests=[gate_test("gate__retired", rows_of(3))])
        first = gate.decide(directory, policy_file, canary_failed=False, decided_at="2026-10-05T00:02:00Z")
        second = gate.decide(directory, policy_file, canary_failed=False, decided_at="2026-10-05T00:02:00Z")
        assert first == second
        assert not (directory / "gate.json").exists()
        with work.history() as hist:
            assert hist.execute("SELECT count(*) FROM obs_gate_decision").fetchone() == (0,)

    def test_decided_at_defaults_to_now_iso_z(self, work, policy_file):
        directory = work.build()
        d = gate.decide(directory, policy_file, canary_failed=False)
        assert d.decided_at.endswith("Z") and "T" in d.decided_at

    def test_frozen(self, work, policy_file):
        d = self.sample(work, policy_file)
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.level = "pass"
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.findings[0].count = 0


class TestWrite:
    def test_writes_gate_json_and_obs_rows(self, work, policy_file):
        directory = work.build(tests=[gate_test("gate__retired", rows_of(30)),
                                      gate_test("info__alias_deferred", rows_of(2))])
        d = gate.decide(directory, policy_file, canary_failed=True, decided_at="2026-10-05T00:02:00Z")
        path = gate.write(d, directory)
        assert path == directory / "gate.json"
        assert json.loads(path.read_text()) == d.to_mapping()
        with work.history() as hist:
            decision = hist.execute("SELECT run_id, revision, level, policy_version, decision_json "
                                    "FROM obs_gate_decision").fetchall()
            assert decision == [(CURRENT_RUN, 5, d.level, d.policy_version, d.to_json())]
            rows = hist.execute("SELECT check_name, scope, level, count FROM obs_gate_finding "
                                "WHERE run_id = ? ORDER BY 1, 2", [CURRENT_RUN]).fetchall()
        assert rows == sorted((f.check, f.scope, f.level, f.count) for f in d.findings)

    def test_observed_at_is_iso_z(self, work, policy_file):
        directory = work.build(tests=[gate_test("gate__retired", rows_of(1))])
        gate.write(gate.decide(directory, policy_file, canary_failed=False, decided_at="2026-10-05T00:02:00Z"), directory)
        with work.history() as hist:
            (at,) = hist.execute("SELECT observed_at FROM obs_gate_decision").fetchone()
            ats = [r[0] for r in hist.execute("SELECT observed_at FROM obs_gate_finding WHERE run_id = ?", [CURRENT_RUN]).fetchall()]
        assert at.endswith("Z") and all(a.endswith("Z") for a in ats)

    def test_finding_rows_carry_the_decided_numbers(self, work, policy_file):
        directory = work.build(tests=[gate_test("gate__retired", rows_of(30))])
        gate.write(gate.decide(directory, policy_file, canary_failed=False, decided_at="2026-10-05T00:02:00Z"), directory)
        with work.history() as hist:
            got = hist.execute("SELECT level, count, population, pct, escalated, consecutive_runs "
                               "FROM obs_gate_finding WHERE check_name = 'retired'").fetchall()
        assert got == [("blocker", 30, 1000, pytest.approx(3.0), True, 1)]

    def test_several_dbt_errors_can_be_written(self, work, policy_file):
        directory = work.build(nodes=[("model.ul_house.mart_stat", "error", "a"),
                                      ("test.ul_house.unique_mart_stat_x.1", "fail", "b"),
                                      ("test.ul_house.not_null_mart_item_name.2", "fail", "c")])
        d = gate.decide(directory, policy_file, canary_failed=False)
        gate.write(d, directory)
        with work.history() as hist:
            assert hist.execute("SELECT count(*) FROM obs_gate_decision").fetchone() == (1,)

    def test_writing_the_same_run_twice_is_refused(self, work, policy_file):
        directory = work.build(tests=[gate_test("gate__retired", rows_of(1))])
        d = gate.decide(directory, policy_file, canary_failed=False, decided_at="2026-10-05T00:02:00Z")
        gate.write(d, directory)
        with pytest.raises(gate.GateError):
            gate.write(d, directory)
        with work.history() as hist:
            assert hist.execute("SELECT count(*) FROM obs_gate_decision").fetchone() == (1,)

    def test_gate_error_is_an_exception(self):
        assert issubclass(gate.GateError, Exception)
