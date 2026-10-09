from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.dbt

duckdb = pytest.importorskip("duckdb")

import dbt_harness as P
from violation_cases import CASES, Case
from ul_house.build import gate

_BASE: dict = {}


def base_history(mini_seed: Path) -> Path:
    """bootstrap history and release rows"""
    if "path" not in _BASE:
        root = P.scratch("violation_base")
        seed = P.copy_seed(mini_seed, root / "seed_base")
        built = P.build(seed, root / "base" / "work", revision=1, started_at="2026-10-05T12:00:00Z")
        path = root / "base_history.duckdb"
        shutil.copyfile(built.history_path, path)
        P.add_release(path, built)
        _BASE["path"] = path
    return _BASE["path"]


def test_cases_are_v01_to_v44():
    ids = {c.id.split("-")[0][:3] for c in CASES}
    assert {f"V{n:02d}" for n in range(1, 45)} <= ids
    assert len({c.id for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_violation(case: Case, offline_dbt, mini_seed_path):
    tmp_path = P.scratch(case.id)
    seed = P.copy_seed(mini_seed_path, tmp_path / "seed")
    if case.mutate_seed:
        case.mutate_seed(seed)
    project = None
    if case.mutate_project:
        project = P.copy_project(tmp_path / "project")
        case.mutate_project(project)
    policy = P.policy_with(tmp_path, case.policy) if case.policy else None
    history = None
    if case.previous:
        history = tmp_path / "history_in.duckdb"
        shutil.copyfile(base_history(mini_seed_path), history)
        if case.mutate_history:
            case.mutate_history(history)
    kwargs = dict(revision=2 if case.previous else 1, history_from=history, mutate_records=case.mutate_records,
                  project_dir=project, select=case.select, policy_path=policy, decide=False,
                  started_at=P.PREVIOUS_RUN_AT if case.previous else "2026-10-06T12:00:00Z")
    if case.raises:
        with pytest.raises(case.raises):
            P.build(seed, tmp_path / "work", **kwargs)
        return

    built = P.build(seed, tmp_path / "work", **kwargs)
    if case.delete_run_results:
        (built.work / "target" / "run_results.build.json").unlink()
    decision = gate.decide(built.work, policy or gate.POLICY_PATH, canary_failed=case.canary_failed)
    failing = built.failing() if not case.delete_run_results else []

    for pattern in case.failing:
        assert any(re.search(pattern, uid) for uid in failing), f"nothing matching {pattern!r} failed; failing: {failing}"
    if case.no_failing:
        assert failing == [], failing
    for check, scope, level in case.findings:
        hits = [f for f in decision.findings if f.check == check and (scope is None or f.scope == scope)]
        assert hits, f"no {check}/{scope} finding in {[(f.check, f.scope, f.level) for f in decision.findings]}"
        assert level in {f.level for f in hits}, [(f.check, f.scope, f.level) for f in hits]
    for part in case.sources:
        assert any(part in src for f in decision.findings for src in f.sources), \
            f"no finding sourced from {part}: {[(f.check, f.sources) for f in decision.findings]}"
    if case.extra:
        case.extra(built, decision)
    assert decision.level in case.levels, (decision.level, [(f.check, f.scope, f.level) for f in decision.findings])
    if "error" in case.levels and len(case.levels) == 1:
        assert not decision.publishable and not decision.held
