"""The build CLI: parsing, wiring, workflows."""
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import release_fixtures as F
from release_fixtures import FakeS3Client, World, base_step, cleanup_tmp

from ul_house.build import cli, release, sign
from ul_house.build.store import Layout, LocalStore, R2Store, sha256_file
from ul_house.seed.store import Store

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
FILES = ("quality", "transform", "contract", "data", "checkpoint", "api-test", "api-deploy")
P19_SECRETS = {"R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT", "RELEASE_SIGNING_KEY",
               "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "WORKFLOW_KEEPALIVE_TOKEN"}
SHA_BUILD, SHA_CATALOG = "a" * 40, "b" * 40


def run(*argv):
    return cli.main([str(a) for a in argv])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """no real env (environ restored)"""
    saved = dict(os.environ)
    monkeypatch.chdir(tmp_path)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def ci_env(tmp_path, monkeypatch):
    """CI output files, no inherited context"""
    out, summary = tmp_path / "gh_output", tmp_path / "gh_summary"
    out.write_text("")
    summary.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    for name in ("GITHUB_EVENT_NAME", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_ACTOR", "UL_HOUSE_STORE",
                 "UL_HOUSE_API_BASE", "RELEASE_SIGNING_KEY", "RELEASE_PUBLIC_KEYS", "UL_HOUSE_DEV_PUBLIC_KEYS"):
        monkeypatch.delenv(name, raising=False)
    from ul_house.sync import keys
    monkeypatch.setattr(keys, "PUBLIC_KEYS", ())
    return SimpleNamespace(output=out, summary=summary)


# parse
def test_stages_are_the_proposals():
    assert cli.STAGES == ("pull", "crawl", "parse", "model", "gate", "release", "publish", "checkpoint", "all",
                          "keygen", "seed-push")
    assert (cli.EXIT_OK, cli.EXIT_FAILED, cli.EXIT_STATE_MISSING, cli.EXIT_CHECK_FAILED, cli.EXIT_USAGE) == (
        0, 1, 2, 4, 64)


@pytest.mark.parametrize("stage", cli.STAGES)
def test_every_stage_accepts_help(stage, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([stage, "--help"])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_an_unknown_stage_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["frobnicate"])
    assert exc.value.code == 64


def test_no_stage_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 64


@pytest.mark.parametrize("flag", ["--allow-dirty", "--skip-check"])
def test_local_only_flags_are_refused_for_r2(flag, ci_env):
    assert run("publish", "--store", "r2", flag) == 64


def test_allow_dirty_with_the_store_taken_from_the_environment(ci_env, monkeypatch):
    monkeypatch.setenv("UL_HOUSE_STORE", "r2")
    assert run("publish", "--allow-dirty") == 64


def test_the_module_runs_as_a_script():
    done = subprocess.run([sys.executable, "-m", "ul_house.build", "--help"], cwd=REPO, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0 and "usage" in done.stdout.lower()


# pull
def seeded_store(tmp_path, *, history: bool = False, current: bool = False) -> LocalStore:
    store = LocalStore(tmp_path / "store")
    w = World(tmp_path / "src")
    base_step(w)
    w.commit()
    store.put_file(Layout.SEED_CURRENT, w.work / "seed.sqlite")
    if history:
        store.put_file(Layout.HISTORY_CURRENT, w.work / "history" / "current.duckdb")
    if current:
        store.put(Layout.CURRENT, b"{}")
    return store


def test_pull_without_a_seed_exits_2_and_says_so(tmp_path, ci_env, capsys):
    LocalStore(tmp_path / "store")
    assert run("pull", "--work", tmp_path / "work", "--store", f"local:{tmp_path / 'store'}") == 2
    assert "seed" in capsys.readouterr().err.lower()


def test_pull_bootstrap_allows_a_missing_history_when_nothing_was_published(tmp_path, ci_env):
    seeded_store(tmp_path)
    code = run("pull", "--work", tmp_path / "work", "--store", f"local:{tmp_path / 'store'}")
    assert code == 0
    assert (tmp_path / "work" / "seed.sqlite").is_file()
    assert json.loads((tmp_path / "work" / "revision.json").read_text())["revision"] == 1


def test_pull_with_a_published_release_but_no_history_exits_2(tmp_path, ci_env, capsys):
    seeded_store(tmp_path, current=True)
    assert run("pull", "--work", tmp_path / "work", "--store", f"local:{tmp_path / 'store'}") == 2
    assert "history" in capsys.readouterr().err.lower()


def test_pull_fetches_both_files_and_numbers_the_next_revision(tmp_path, ci_env):
    w = World(tmp_path / "w")
    w.cycle(base_step)
    store_dir = w.store.root
    work = tmp_path / "work"
    assert run("pull", "--work", work, "--store", f"local:{store_dir}") == 0
    assert (work / "seed.sqlite").read_bytes() == (store_dir / "build/seed/current.sqlite").read_bytes()
    assert (work / "history" / "current.duckdb").is_file()
    assert json.loads((work / "revision.json").read_text())["revision"] == 2


# crawl
@pytest.fixture
def crawl_env(tmp_path, ci_env, monkeypatch):
    calls = []
    returns = {"code": 0}

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returns["code"])
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(release.BuildInfo, "collect", classmethod(
        lambda cls, root, environ=None: release.BuildInfo(SHA_BUILD, False, "run-x", {})))
    monkeypatch.setattr(cli, "_git_line", lambda *argv: SHA_CATALOG)
    return SimpleNamespace(calls=calls, returns=returns, work=tmp_path / "work")


def pairs(cmd):
    return {a: b for a, b in zip(cmd, cmd[1:]) if a.startswith("--")}


def test_crawl_calls_the_crawler_entry_point_with_p9_flags(crawl_env):
    assert run("crawl", "--work", crawl_env.work, "--limit", "5") == 0
    (cmd,) = crawl_env.calls
    assert cmd[:4] == [sys.executable, "-m", "ul_house.crawl.run", "--seed"]
    flags = pairs(cmd)
    assert flags["--seed"] == str(crawl_env.work / "seed.sqlite")
    assert flags["--trigger"] == "manual"
    assert flags["--code-commit"] == SHA_BUILD and flags["--catalog-commit"] == SHA_CATALOG
    assert flags["--limit"] == "5"


@pytest.mark.parametrize("event, trigger", [("schedule", "schedule"), ("workflow_dispatch", "dispatch"),
                                            ("push", "manual"), ("", "manual")])
def test_crawl_trigger_comes_from_the_github_event(crawl_env, monkeypatch, event, trigger):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    run("crawl", "--work", crawl_env.work)
    assert pairs(crawl_env.calls[0])["--trigger"] == trigger


def test_crawl_passes_the_optional_flags_through(crawl_env, tmp_path):
    run("crawl", "--work", crawl_env.work, "--resume", "--init", "--base-url", "http://127.0.0.1:9",
        "--catalog", tmp_path / "catalog.toml")
    cmd = crawl_env.calls[0]
    assert "--resume" in cmd and "--init" in cmd
    flags = pairs(cmd)
    assert flags["--base-url"] == "http://127.0.0.1:9" and flags["--catalog"] == str(tmp_path / "catalog.toml")


@pytest.mark.parametrize("code, expected", [(0, 0), (1, 1), (130, 1), (2, 2)])
def test_crawl_exit_codes_map(crawl_env, code, expected):
    crawl_env.returns["code"] = code
    assert run("crawl", "--work", crawl_env.work) == expected


def test_crawl_never_imports_the_crawler_into_the_build_process():
    source = (REPO / "src" / "ul_house" / "build" / "cli.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+ul_house\.(crawl|http)\b", source, re.M)


# stages
def test_parse_calls_load_seed(tmp_path, ci_env, monkeypatch):
    from ul_house.build import load
    seen = []
    monkeypatch.setattr(load, "load_seed", lambda seed, work_dir, **kw: seen.append((Path(seed), Path(work_dir))))
    assert run("parse", "--work", tmp_path / "work") == 0
    assert seen == [(tmp_path / "work" / "seed.sqlite", tmp_path / "work")]


def test_parse_failure_is_exit_1(tmp_path, ci_env, monkeypatch, capsys):
    from ul_house.build import load

    def boom(*a, **kw):
        raise RuntimeError("bad seed")
    monkeypatch.setattr(load, "load_seed", boom)
    assert run("parse", "--work", tmp_path / "work") == 1
    assert "bad seed" in capsys.readouterr().err


def test_model_calls_run_dbt_with_the_revision_from_pull(tmp_path, ci_env, monkeypatch):
    from ul_house.build import dbt
    work = tmp_path / "work"
    work.mkdir()
    (work / "revision.json").write_text(json.dumps({"revision": 7, "bootstrap": False}))
    seen = {}

    def fake_run_dbt(work_dir, *, revision, scope, policy_path, **kw):
        seen.update(work_dir=Path(work_dir), revision=revision, policy_path=Path(policy_path))
        return "transform-7"
    monkeypatch.setattr(dbt, "run_dbt", fake_run_dbt)
    assert run("model", "--work", work, "--store", f"local:{tmp_path / 'store'}") == 0
    assert seen["work_dir"] == work and seen["revision"] == 7
    assert seen["policy_path"].name == "gate_policy.toml"


class FakeDecision:
    def __init__(self, level, revision=3):
        self.level, self.revision = level, revision

    def to_mapping(self):
        return {"level": self.level, "policy_version": F.POLICY_VERSION, "policy_commit": F.COMMIT, "findings": [
            {"check": "icon_missing", "scope": "new_item", "level": "warning", "count": 1, "pct": 0.1,
             "message": "m"}] if self.level != "pass" else []}


@pytest.fixture
def fake_gate(monkeypatch):
    from ul_house.build import gate
    seen = {}

    def decide(work_dir, policy_path, *, canary_failed):
        seen.update(work_dir=Path(work_dir), canary_failed=canary_failed)
        return FakeDecision(seen["level"])
    monkeypatch.setattr(gate, "decide", decide)
    monkeypatch.setattr(gate, "write", None, raising=False)          # gate.json
    return seen


@pytest.mark.parametrize("level, code", [("pass", 0), ("warning", 0), ("blocker", 0), ("error", 1)])
def test_gate_levels_and_exit_codes(tmp_path, ci_env, fake_gate, level, code):
    fake_gate["level"] = level
    work = tmp_path / "work"
    assert run("gate", "--work", work) == code
    written = json.loads((work / "gate.json").read_text())
    assert written["level"] == level and written["policy_commit"] == F.COMMIT


def test_gate_passes_the_canary_flag(tmp_path, ci_env, fake_gate):
    fake_gate["level"] = "pass"
    run("gate", "--work", tmp_path / "work")
    assert fake_gate["canary_failed"] is None
    run("gate", "--work", tmp_path / "work", "--canary-failed")
    assert fake_gate["canary_failed"] is True
    run("gate", "--work", tmp_path / "work", "--canary-passed")
    assert fake_gate["canary_failed"] is False
    with pytest.raises(SystemExit) as both:
        run("gate", "--work", tmp_path / "work", "--canary-failed", "--canary-passed")
    assert both.value.code == 64


def test_gate_error_writes_an_outcome(tmp_path, ci_env, fake_gate):
    fake_gate["level"] = "error"
    run("gate", "--work", tmp_path / "work")
    assert json.loads((tmp_path / "work" / "outcome.json").read_text())["outcome"] == "gate_error"


def test_gate_summary_line(tmp_path, ci_env, fake_gate):
    fake_gate["level"] = "warning"
    run("gate", "--work", tmp_path / "work")
    assert re.search(r"^Gate\s+warning", ci_env.summary.read_text(), re.M)


# all stages
def record_stages(monkeypatch, *, fail_at=None):
    order = []
    for name in cli.ALL_ORDER:
        def stage(args, name=name):
            order.append(name)
            return 1 if name == fail_at else 0
        monkeypatch.setattr(cli, f"_stage_{name}", stage)
    return order


def test_all_runs_the_stages_in_order(ci_env, monkeypatch, tmp_path):
    order = record_stages(monkeypatch)
    assert run("all", "--work", tmp_path / "w") == 0
    assert order == ["pull", "crawl", "parse", "model", "gate", "release", "publish"]


@pytest.mark.parametrize("failing", ["crawl", "model", "gate", "release"])
def test_all_stops_at_the_first_failing_stage(ci_env, monkeypatch, tmp_path, failing):
    order = record_stages(monkeypatch, fail_at=failing)
    assert run("all", "--work", tmp_path / "w") == 1
    assert order == list(cli.ALL_ORDER[: cli.ALL_ORDER.index(failing) + 1])


def test_all_returns_the_failing_stages_exit_code(ci_env, monkeypatch, tmp_path):
    record_stages(monkeypatch)
    monkeypatch.setattr(cli, "_stage_crawl", lambda args: 2)
    assert run("all", "--work", tmp_path / "w") == 2


# release + publish
@pytest.fixture
def signing(ci_env, monkeypatch):
    pem, public = sign.generate_keypair()
    monkeypatch.setenv("RELEASE_SIGNING_KEY", pem.decode())
    monkeypatch.setenv("RELEASE_PUBLIC_KEYS", public)
    return public


def new_world(path: Path) -> World:
    """world signed with env key"""
    w = World(path)
    w._signer = sign.Signer.from_env()
    return w


def stage_args(w: World, *extra):
    return ("--work", w.work, "--store", f"local:{w.store.root}", *extra)


def gh_outputs(ci_env) -> list[str]:
    return [line for line in ci_env.output.read_text().splitlines() if line]


def test_release_then_publish_publishes_revision_one(tmp_path, signing, ci_env):
    w = new_world(tmp_path / "w")
    base_step(w)
    w.commit()
    w.write_gate()
    assert run("release", *stage_args(w)) == 0
    decision = json.loads((w.work / "release" / "decision.json").read_text())
    assert decision["action"] == "publish" and decision["revision"] == 1
    assert run("publish", *stage_args(w, "--skip-check", "--allow-dirty")) == 0
    current = json.loads((w.store.root / "pub" / "current.json").read_bytes())
    assert current["revision"] == 1 and sign.verify(current, [signing]) == 0
    assert (w.store.root / "pub" / "manifests" / "1.json").is_file()
    assert "outcome=published" in gh_outputs(ci_env)
    outcome = json.loads((w.work / "outcome.json").read_text())
    assert outcome["outcome"] == "published" and outcome["revision"] == 1
    text = ci_env.summary.read_text()
    assert re.search(r"^Release\s+revision 1", text, re.M)
    assert re.search(r"^Publish\s+published", text, re.M) and "current.json" in text


def dev_key_world(tmp_path, signing, monkeypatch):
    """released world with a dev key"""
    from ul_house.sync import keys
    monkeypatch.setattr(keys, "PUBLIC_KEYS", ("ab" * 32,))
    monkeypatch.setenv("UL_HOUSE_DEV_PUBLIC_KEYS", signing)
    w = new_world(tmp_path / "w")
    base_step(w)
    w.commit()
    w.write_gate()
    assert run("release", *stage_args(w)) == 0
    return w


def test_publish_trusts_a_dev_key_on_a_loopback_api_base(tmp_path, signing, ci_env, monkeypatch):
    w = dev_key_world(tmp_path, signing, monkeypatch)
    assert run("publish", *stage_args(w, "--skip-check", "--allow-dirty", "--api-base", "http://127.0.0.1:9")) == 0
    current = json.loads((w.store.root / "pub" / "current.json").read_bytes())
    assert sign.verify(current, [signing]) == 0


def test_publish_ignores_the_dev_key_off_loopback(tmp_path, signing, ci_env, monkeypatch, capsys):
    w = dev_key_world(tmp_path, signing, monkeypatch)
    code = run("publish", *stage_args(w, "--skip-check", "--allow-dirty", "--api-base", "https://api.example.invalid"))
    err = capsys.readouterr().err
    assert code == 1
    assert "SignatureError" in err and "is ignored" in err
    assert not (w.store.root / "pub" / "current.json").exists()


def test_publish_ignores_the_dev_key_without_an_api_base(tmp_path, signing, ci_env, monkeypatch):
    w = dev_key_world(tmp_path, signing, monkeypatch)
    assert run("publish", *stage_args(w, "--skip-check", "--allow-dirty")) == 1
    assert not (w.store.root / "pub" / "current.json").exists()


def test_publish_without_an_api_base_or_skip_check_is_refused_before_anything_goes_live(tmp_path, signing, ci_env):
    w = new_world(tmp_path / "w")
    base_step(w)
    w.commit()
    w.write_gate()
    assert run("release", *stage_args(w)) == 0
    assert run("publish", *stage_args(w, "--allow-dirty")) == 64
    assert not (w.store.root / "pub" / "current.json").exists()


def test_publish_to_a_non_local_store_refuses_a_build_without_a_clean_commit(tmp_path, signing, ci_env, monkeypatch):
    class R2Like(LocalStore):
        kind = "r2"
    w = new_world(tmp_path / "w")
    base_step(w)
    w.commit()
    w.write_gate()
    assert run("release", *stage_args(w)) == 0                          # local build explicitly
    remote = R2Like(w.store.root)
    monkeypatch.setattr(cli, "_store", lambda args: remote)
    assert run("publish", *stage_args(w), "--api-base", "https://example.invalid") == cli.EXIT_FAILED
    assert not (w.store.root / "pub" / "current.json").exists()


def test_a_blocker_holds_the_candidate_and_exits_zero(tmp_path, signing, ci_env):
    w = new_world(tmp_path / "w")
    w.cycle(base_step)
    w.gate("blocker")
    w.update_equipment("1001", name="B1")
    w.commit()
    w.write_gate()
    assert run("release", *stage_args(w)) == 0
    assert run("publish", *stage_args(w, "--skip-check", "--allow-dirty")) == 0
    assert "outcome=held" in gh_outputs(ci_env)
    assert (w.store.root / "candidates" / "2" / "bundle.json").is_file()
    assert json.loads((w.store.root / "pub" / "current.json").read_bytes())["revision"] == 1
    assert re.search(r"^Publish\s+held", ci_env.summary.read_text(), re.M)


def test_no_changes_is_a_heartbeat(tmp_path, signing, ci_env):
    w = new_world(tmp_path / "w")
    w.cycle(base_step)
    w.commit()
    w.write_gate()
    assert run("release", *stage_args(w)) == 0
    assert run("publish", *stage_args(w, "--skip-check", "--allow-dirty")) == 0
    assert "outcome=heartbeat" in gh_outputs(ci_env)
    assert json.loads((w.store.root / "pub" / "current.json").read_bytes())["revision"] == 1
    assert re.search(r"^Release\s+no changes", ci_env.summary.read_text(), re.M)


def test_approve_publishes_the_held_candidate(tmp_path, signing, ci_env):
    w = new_world(tmp_path / "w")
    w.cycle(base_step)
    w.gate("blocker")
    w.update_equipment("1001", name="B1")
    w.commit()
    w.write_gate()
    run("release", *stage_args(w))
    run("publish", *stage_args(w, "--skip-check", "--allow-dirty"))
    code = run("publish", *stage_args(w, "--skip-check", "--allow-dirty", "--approve", 2, "--reason", "looked fine",
                                      "--approver", "octocat"))
    assert code == 0
    assert json.loads((w.store.root / "pub" / "current.json").read_bytes())["revision"] == 2
    assert not list((w.store.root / "candidates").rglob("*.json"))
    assert "approved by octocat" in ci_env.summary.read_text()


def test_approve_requires_a_reason(tmp_path, signing, ci_env):
    w = new_world(tmp_path / "w")
    w.cycle(base_step)
    assert run("publish", *stage_args(w, "--skip-check", "--approve", 2)) == 64


def test_a_gate_error_release_is_exit_1(tmp_path, signing, ci_env, capsys):
    w = new_world(tmp_path / "w")
    w.cycle(base_step)
    w.gate("error")
    w.update_equipment("1001", name="B1")
    w.commit()
    w.write_gate()
    assert run("release", *stage_args(w)) == 1
    assert "release failed" in capsys.readouterr().err


def test_publish_without_a_signing_key_is_exit_1(tmp_path, signing, ci_env, monkeypatch, capsys):
    w = new_world(tmp_path / "w")
    base_step(w)
    w.commit()
    w.write_gate()
    run("release", *stage_args(w))
    monkeypatch.delenv("RELEASE_SIGNING_KEY")
    assert run("publish", *stage_args(w, "--skip-check", "--allow-dirty")) == 1
    assert "RELEASE_SIGNING_KEY" in capsys.readouterr().err
    assert not (w.store.root / "pub" / "current.json").exists()


def test_checkpoint_stage(tmp_path, ci_env, monkeypatch, capsys):
    from ul_house.build import checkpoint
    seen = []
    result = checkpoint.CheckpointResult("20270102T030000Z", "2027-Q1", "a" * 64, "b" * 64,
                                         {"dataset_changes": {"before": 5, "after": 3, "archived": 2}})
    monkeypatch.setattr(checkpoint, "run_checkpoint", lambda store, work, **kw: seen.append(Path(work)) or result)
    assert run("checkpoint", "--work", tmp_path / "w", "--store", f"local:{tmp_path / 'store'}") == 0
    assert seen == [tmp_path / "w"] and "2027-Q1" in capsys.readouterr().out


def test_checkpoint_failure_is_exit_1(tmp_path, ci_env, monkeypatch):
    from ul_house.build import checkpoint

    def boom(*a, **kw):
        raise checkpoint.CheckpointError("rows do not reconcile")
    monkeypatch.setattr(checkpoint, "run_checkpoint", boom)
    assert run("checkpoint", "--work", tmp_path / "w", "--store", f"local:{tmp_path / 'store'}") == 1


# out/summary
def test_write_outcome_writes_the_file_and_the_github_output(tmp_path, ci_env):
    cli.write_outcome(tmp_path / "w", "held", revision=4)
    assert json.loads((tmp_path / "w" / "outcome.json").read_text()) == {"outcome": "held", "revision": 4}
    assert gh_outputs(ci_env) == ["outcome=held"]


def test_write_outcome_without_github_output(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    cli.write_outcome(tmp_path / "w", "heartbeat", revision=2)
    assert json.loads((tmp_path / "w" / "outcome.json").read_text())["outcome"] == "heartbeat"


def test_render_summary_of_an_empty_work_dir_is_empty(tmp_path):
    assert cli.render_summary(tmp_path / "nothing") == ""


# keygen
def test_keygen_writes_the_key_files_and_prints_only_the_public_key(tmp_path, ci_env, capsys):
    out = tmp_path / "keys"
    assert run("keygen", "--out", out) == 0
    captured = capsys.readouterr()
    pem_path, hex_path = out / "signing.pem", out / "public.hex"
    assert stat.S_IMODE(pem_path.stat().st_mode) == 0o600
    printed = captured.out.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", printed)
    assert hex_path.read_text().strip() == printed
    assert "PRIVATE KEY" not in captured.out + captured.err
    assert pem_path.read_text().startswith("-----BEGIN PRIVATE KEY-----")
    signer = sign.Signer.from_pem(pem_path.read_bytes())
    assert signer.public_key_hex() == printed


def test_keygen_never_overwrites_an_existing_key(tmp_path, ci_env):
    out = tmp_path / "keys"
    run("keygen", "--out", out)
    before = (out / "signing.pem").read_bytes()
    assert run("keygen", "--out", out) == 64
    assert (out / "signing.pem").read_bytes() == before


# workflow
def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text())


def triggers(doc: dict) -> dict:
    return doc.get("on", doc.get(True))                         # PyYAML loads key `on` as True


def steps(doc: dict, job: str | None = None) -> list[dict]:
    jobs = doc["jobs"]
    return jobs[job or next(iter(jobs))]["steps"]


def all_runs(doc: dict) -> list[str]:
    return [s["run"] for job in doc["jobs"].values() for s in job["steps"] if "run" in s]


def test_all_seven_workflows_exist_and_parse():
    for name in FILES:
        doc = load(name)
        assert doc["name"] == name and doc["jobs"], name


def test_data_workflow_trigger_inputs_and_concurrency():
    doc = load("data")
    t = triggers(doc)
    assert [s["cron"] for s in t["schedule"]] == ["17 23 * * *"]
    assert set(t["workflow_dispatch"]["inputs"]) == {"approve_candidate", "reason", "force_snapshot"}
    assert doc["concurrency"]["group"] == "data" and doc["concurrency"]["cancel-in-progress"] is False


def test_data_workflow_pulls_before_it_crawls_and_in_stage_order():
    runs = [s.get("run", "") for s in steps(load("data"))]
    index = {}
    for stage in ("pull", "crawl", "parse", "model", "gate", "release", "publish"):
        index[stage] = next(i for i, r in enumerate(runs) if re.search(rf"\$BUILD {stage}\b", r) and "--approve" not in r)
    assert [index[s] for s in ("pull", "crawl", "parse", "model", "gate", "release", "publish")] == sorted(index.values())


def test_data_workflow_keeps_the_schedule_alive():
    doc = load("data")
    keepalive = [s for s in steps(doc) if "WORKFLOW_KEEPALIVE_TOKEN" in json.dumps(s)]
    assert keepalive and "workflows/data.yml/enable" in keepalive[0]["run"]
    assert "schedule" in keepalive[0]["if"]


def test_the_signing_key_reaches_only_steps_that_run_the_publish_stage():
    for name in FILES:
        doc = load(name)
        top_env = json.dumps(doc.get("env", {}))
        assert "RELEASE_SIGNING_KEY" not in top_env, name
        for job in doc["jobs"].values():
            assert "RELEASE_SIGNING_KEY" not in json.dumps(job.get("env", {})), name
            for step in job["steps"]:
                if "RELEASE_SIGNING_KEY" in json.dumps({k: v for k, v in step.items() if k != "run"}):
                    assert re.search(r"\$BUILD publish\b|ul_house\.build publish\b", step["run"]), (name, step)
                    assert "RELEASE_SIGNING_KEY" in step.get("env", {}), (name, step)
                assert "RELEASE_SIGNING_KEY" not in step.get("run", ""), (name, step)
    publishing = [s for s in steps(load("data")) if "RELEASE_SIGNING_KEY" in s.get("env", {})]
    assert len(publishing) == 2                                                  # publish, and approve-and-publish


def test_no_workflow_interpolates_inputs_or_secrets_into_a_run_script():
    for name in FILES:
        for script in all_runs(load(name)):
            assert "${{ inputs." not in script and "${{ github.event.inputs" not in script, (name, script)
            assert "${{ secrets." not in script, (name, script)


def test_workflows_reference_only_the_placeholder_secrets_and_variables():
    for name in FILES:
        text = (WORKFLOWS / f"{name}.yml").read_text()
        for secret in re.findall(r"secrets\.([A-Za-z0-9_]+)", text):
            assert secret in P19_SECRETS | {"GITHUB_TOKEN"}, (name, secret)
        for var in re.findall(r"\bvars\.([A-Za-z0-9_]+)", text):
            assert var == "UL_HOUSE_API_BASE", (name, var)


def test_every_build_stage_a_workflow_calls_exists():
    for name in FILES:
        text = (WORKFLOWS / f"{name}.yml").read_text()
        for stage in re.findall(r"(?:ul_house\.build|\$BUILD)[ \t]+([a-z]+)", text):
            assert stage in cli.STAGES, (name, stage)


def test_checkpoint_workflow_shares_the_data_concurrency_group():
    doc = load("checkpoint")
    assert doc["concurrency"]["group"] == "data"
    assert [s["cron"] for s in triggers(doc)["schedule"]] == ["41 3 2 1,4,7,10 *"]
    assert "workflow_dispatch" in triggers(doc)
    assert any(re.search(r"\$BUILD checkpoint\b", r) for r in all_runs(doc))


def test_quality_workflow_lints_for_real_and_types_advisorily():
    doc = load("quality")
    t = triggers(doc)
    assert "push" in t and "pull_request" in t
    by_name = {s.get("name"): s for s in steps(doc)}
    assert "ruff check" in by_name["Lint"]["run"] and not by_name["Lint"].get("continue-on-error")
    assert "mypy" in by_name["Types"]["run"] and by_name["Types"]["continue-on-error"] is True
    pytest_step = next(s for s in steps(doc) if "pytest" in s.get("run", ""))
    assert "--no-fetch" in pytest_step["run"]
    for ignored in ("test_release", "test_publish", "test_checkpoint", "test_cli"):
        assert f"tests/{ignored}.py" in pytest_step["run"]


def test_quality_workflow_ignores_every_test_that_needs_the_build_group():
    """quality ignores tests importing duckdb"""
    text = " ".join(all_runs(load("quality")))
    heavy = [p.name for p in sorted((REPO / "tests").glob("test_*.py"))
             if re.search(r"^\s*(import duckdb|from duckdb|import release_fixtures|from release_fixtures)",
                          p.read_text(encoding="utf-8"), re.M)]
    assert "test_object_store.py" in heavy
    for name in heavy:
        assert f"--ignore=tests/{name}" in text, name


def test_transform_workflow_runs_the_build_tests():
    run_text = " ".join(all_runs(load("transform")))
    for test in ("test_release", "test_sign", "test_publish", "test_checkpoint", "test_cli", "test_object_store"):
        assert f"tests/{test}.py" in run_text, test
    assert "-m dbt" in run_text


def test_api_workflows():
    test = load("api-test")
    assert set(triggers(test)["push"]["paths"]) == {"api/**"}
    deploy = load("api-deploy")
    assert deploy["jobs"]["deploy"]["needs"] == "test"
    assert deploy["jobs"]["deploy"]["concurrency"]["group"] == "api-deploy"
    text = json.dumps(deploy)
    for needle in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "pywrangler deploy", "-m live",
                   "UL_HOUSE_API_EXPECTED_COMMIT"):
        assert needle in text, needle


def test_contract_workflow_runs_on_a_schedule_and_files_an_issue_on_failure():
    doc = load("contract")
    assert [s["cron"] for s in triggers(doc)["schedule"]] == ["0 21 * * *"]
    assert any("gh issue create" in r for r in all_runs(doc))


def test_actions_are_pinned_to_major_versions():
    for name in FILES:
        for job in load(name)["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", step["uses"]), (name, step["uses"])


# env + seed-push
@pytest.fixture
def offline(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    for name in ("UL_HOUSE_STORE", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
                 "UL_HOUSE_API_BASE", "SEEDPUSH_X"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def make_seed(path, *, status="complete", in_flight=False):
    with Store.open(path) as store:
        run_id = store.start_run(trigger="manual", code_commit="a" * 40, catalog_version="v",
                                 catalog_commit="b" * 40, shard_index=None, code_dirty=True)
        if status != "running":
            store.finish_run(run_id, status)
        if in_flight:
            store.conn.execute(
                "INSERT INTO frontier (item_id, source, entry_kind, keep_reason, state, discovered_at, updated_at) "
                "VALUES ('1', 'list', 'catalog', 'x', 'in_flight', 't', 't')")
    return path


@pytest.fixture
def seed(tmp_path, offline):
    return make_seed(tmp_path / "seed.sqlite")


# env
@pytest.mark.usefixtures("offline")
def test_parse_env_file_forms():
    text = "# c\n\nA=1\nexport B=two\nC='x y'\nD=\"q\"\nE=\nbad line\n1X=no\nF = spaced \nG='half\n"
    assert cli.parse_env_file(text) == {"A": "1", "B": "two", "C": "x y", "D": "q", "E": "", "F": "spaced",
                                        "G": "'half"}


@pytest.mark.parametrize("escaped", [False, True])
@pytest.mark.usefixtures("offline")
def test_pem_value_round_trips(tmp_path, monkeypatch, escaped):
    monkeypatch.delenv("RELEASE_SIGNING_KEY", raising=False)
    pem, public = sign.generate_keypair()
    body = pem.decode().strip()
    value = body.replace("\n", "\\n") if escaped else body
    parsed = cli.parse_env_file(f'A=1\nRELEASE_SIGNING_KEY="{value}"\nB=2\n')
    assert parsed["A"] == "1" and parsed["B"] == "2"
    assert parsed["RELEASE_SIGNING_KEY"] == value
    assert sign.Signer.from_env({"RELEASE_SIGNING_KEY": parsed["RELEASE_SIGNING_KEY"]}).public_key_hex() == public
    (tmp_path / ".env").write_text(f'RELEASE_SIGNING_KEY="{value}"\n')
    assert cli.load_env_file(SimpleNamespace(env_file=None, no_env_file=False)) is None
    assert sign.Signer.from_env().public_key_hex() == public


@pytest.mark.usefixtures("offline")
def test_env_file_default_is_dot_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SEEDPUSH_X=from-file\n")
    assert cli.load_env_file(SimpleNamespace(env_file=None, no_env_file=False)) is None
    assert cli.os.environ["SEEDPUSH_X"] == "from-file"
    monkeypatch.delenv("SEEDPUSH_X")


@pytest.mark.usefixtures("offline")
def test_real_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SEEDPUSH_X", "real")
    f = tmp_path / "e.env"
    f.write_text("SEEDPUSH_X=file\n")
    cli.load_env_file(SimpleNamespace(env_file=str(f), no_env_file=False))
    assert cli.os.environ["SEEDPUSH_X"] == "real"


@pytest.mark.usefixtures("offline")
def test_missing_default_file_is_fine_and_explicit_one_is_usage_error(tmp_path):
    assert cli.load_env_file(SimpleNamespace(env_file=None, no_env_file=False)) is None
    assert cli.load_env_file(SimpleNamespace(env_file=str(tmp_path / "nope"), no_env_file=False)) == 64


@pytest.mark.parametrize("kw", [{"env_file": "", "no_env_file": False}, {"env_file": None, "no_env_file": True}])
@pytest.mark.usefixtures("offline")
def test_env_file_can_be_disabled(tmp_path, kw):
    (tmp_path / ".env").write_text("SEEDPUSH_X=file\n")
    assert cli.load_env_file(SimpleNamespace(**kw)) is None
    assert "SEEDPUSH_X" not in cli.os.environ


@pytest.mark.usefixtures("offline")
def test_env_file_configures_the_store_without_printing_values(tmp_path, seed, capsys, monkeypatch):
    (tmp_path / ".env").write_text(f"UL_HOUSE_STORE=local:{tmp_path / 's'}\nR2_SECRET_ACCESS_KEY=topsecret\n")
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    assert run("seed-push", "--seed", seed) == 0
    out = capsys.readouterr()
    assert (tmp_path / "s" / Layout.SEED_CURRENT).is_file()
    assert "topsecret" not in out.out + out.err
    monkeypatch.delenv("UL_HOUSE_STORE", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)


# seed-push, local store
@pytest.mark.usefixtures("offline")
def test_push_writes_and_verifies(tmp_path, seed, capsys):
    store_dir = tmp_path / "store"
    assert run("seed-push", "--seed", seed, "--store", f"local:{store_dir}") == 0
    info = LocalStore(store_dir).head(Layout.SEED_CURRENT)
    assert info is not None and info.sha256 in capsys.readouterr().out
    with sqlite3.connect(store_dir / Layout.SEED_CURRENT) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)


@pytest.mark.usefixtures("offline")
def test_push_folds_the_wal(tmp_path):
    path = make_seed(tmp_path / "w.sqlite")
    live = sqlite3.connect(path)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("UPDATE meta SET value = value WHERE key = 'seed.schema_version'")
    live.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    live.commit()
    store_dir = tmp_path / "store"
    assert run("seed-push", "--seed", path, "--store", f"local:{store_dir}") == 0
    with sqlite3.connect(store_dir / Layout.SEED_CURRENT) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key = 'k'").fetchone() == ("v",)
    live.close()


@pytest.mark.usefixtures("offline")
def test_dry_run_writes_nothing(tmp_path, seed, capsys):
    store_dir = tmp_path / "store"
    assert run("seed-push", "--seed", seed, "--store", f"local:{store_dir}", "--dry-run") == 0
    assert LocalStore(store_dir).head(Layout.SEED_CURRENT) is None
    out = capsys.readouterr().out
    assert Layout.SEED_CURRENT in out and "sha256" in out and "local" in out


@pytest.mark.usefixtures("offline")
def test_existing_object_is_reported(tmp_path, seed, capsys):
    store = LocalStore(tmp_path / "store")
    store.put(Layout.SEED_CURRENT, b"old")
    assert run("seed-push", "--seed", seed, "--store", f"local:{tmp_path / 'store'}", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "3 bytes" in out and "identical to local: no" in out
    assert store.get(Layout.SEED_CURRENT) == b"old"


@pytest.mark.usefixtures("offline")
def test_identical_seed_is_not_uploaded_again(tmp_path, seed, monkeypatch, capsys):
    store = LocalStore(tmp_path / "store")
    assert run("seed-push", "--seed", seed, "--store", f"local:{store.root}") == 0
    capsys.readouterr()
    monkeypatch.setattr(LocalStore, "put_file", lambda *a, **k: pytest.fail("re-uploaded"))
    assert run("seed-push", "--seed", seed, "--store", f"local:{store.root}") == 0
    assert "store already has this seed" in capsys.readouterr().out


@pytest.mark.usefixtures("offline")
def test_different_seed_overwrites(tmp_path, seed, capsys):
    store = LocalStore(tmp_path / "store")
    store.put(Layout.SEED_CURRENT, b"old")
    assert run("seed-push", "--seed", seed, "--store", f"local:{store.root}") == 0
    assert "uploaded and verified" in capsys.readouterr().out
    assert store.get(Layout.SEED_CURRENT)[:16] == b"SQLite format 3\x00"


@pytest.mark.usefixtures("offline")
def test_dirty_code_run_is_not_a_refusal(tmp_path, seed):
    assert run("seed-push", "--seed", seed, "--store", f"local:{tmp_path / 's'}") == 0


@pytest.mark.usefixtures("offline")
def test_requires_seed_argument(tmp_path):
    assert run("seed-push", "--store", f"local:{tmp_path / 's'}") == 64


@pytest.mark.usefixtures("offline")
def test_missing_seed_is_refused(tmp_path, capsys):
    assert run("seed-push", "--seed", tmp_path / "none.sqlite", "--store", f"local:{tmp_path / 's'}") == 2
    assert "not found" in capsys.readouterr().err


@pytest.mark.usefixtures("offline")
def test_corrupt_seed_is_refused(tmp_path, seed, capsys):
    data = bytearray(seed.read_bytes())
    data[:16] = b"not a sqlite db!"
    seed.write_bytes(bytes(data))
    assert run("seed-push", "--seed", seed, "--store", f"local:{tmp_path / 's'}") == 64
    assert not (tmp_path / "s" / Layout.SEED_CURRENT).exists()


@pytest.mark.usefixtures("offline")
def test_wrong_schema_version_is_refused(tmp_path, seed, capsys):
    with sqlite3.connect(seed) as conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'seed.schema_version'")
    assert run("seed-push", "--seed", seed, "--store", f"local:{tmp_path / 's'}") == 64
    assert "schema" in capsys.readouterr().err


@pytest.mark.usefixtures("offline")
def test_in_flight_frontier_is_refused(tmp_path, capsys):
    seed = make_seed(tmp_path / "f.sqlite", in_flight=True)
    assert run("seed-push", "--seed", seed, "--store", f"local:{tmp_path / 's'}") == 64
    assert "in_flight" in capsys.readouterr().err


@pytest.mark.parametrize("status", ["running", "partial", "failed"])
@pytest.mark.usefixtures("offline")
def test_no_complete_run_is_refused(tmp_path, status, capsys):
    seed = make_seed(tmp_path / "r.sqlite", status=status)
    assert run("seed-push", "--seed", seed, "--store", f"local:{tmp_path / 's'}") == 64
    assert "no complete" in capsys.readouterr().err


@pytest.mark.usefixtures("offline")
def test_seed_file_is_left_untouched(tmp_path, seed):
    before = sha256_file(seed)
    run("seed-push", "--seed", seed, "--store", f"local:{tmp_path / 's'}")
    assert sha256_file(seed) == before


@pytest.mark.usefixtures("offline")
def test_upload_failure_exits_nonzero(tmp_path, seed, monkeypatch, capsys):
    store = LocalStore(tmp_path / "s")
    monkeypatch.setattr(store, "put_file", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(cli, "open_store", lambda uri: store)
    assert run("seed-push", "--seed", seed) == 1
    assert "seed-push failed: OSError" in capsys.readouterr().err


@pytest.mark.usefixtures("offline")
def test_seed_push_is_manual_only():
    assert "seed-push" in cli.STAGES and "seed-push" not in cli.ALL_ORDER


# seed-push, r2 over fake client
@pytest.mark.usefixtures("offline")
def test_r2_put_call_and_verify(tmp_path, seed, monkeypatch, capsys):
    client = FakeS3Client()
    monkeypatch.setattr(cli, "open_store", lambda uri: R2Store(client, "ul-house"))
    assert run("seed-push", "--seed", seed, "--store", "r2") == 0
    stored = client.objects[("ul-house", Layout.SEED_CURRENT)]
    assert stored["type"] == "application/vnd.sqlite3"
    assert stored["meta"]["sha256"] in capsys.readouterr().out
    assert [c for c in client.calls if c[0] == "put_object"] == [("put_object", Layout.SEED_CURRENT)]
    assert ("get_object", Layout.SEED_CURRENT) in client.calls


@pytest.mark.usefixtures("offline")
def test_r2_identical_seed_is_not_put_again(tmp_path, seed, monkeypatch, capsys):
    client = FakeS3Client()
    monkeypatch.setattr(cli, "open_store", lambda uri: R2Store(client, "ul-house"))
    assert run("seed-push", "--seed", seed, "--store", "r2") == 0
    assert run("seed-push", "--seed", seed, "--store", "r2") == 0
    assert [c for c in client.calls if c[0] == "put_object"] == [("put_object", Layout.SEED_CURRENT)]
    assert "store already has this seed" in capsys.readouterr().out


@pytest.mark.usefixtures("offline")
def test_r2_dry_run_names_bucket_and_puts_nothing(tmp_path, seed, monkeypatch, capsys):
    client = FakeS3Client()
    monkeypatch.setattr(cli, "open_store", lambda uri: R2Store(client, "ul-house"))
    assert run("seed-push", "--seed", seed, "--store", "r2", "--dry-run") == 0
    assert not any(c[0] == "put_object" for c in client.calls)
    assert "bucket ul-house" in capsys.readouterr().out


@pytest.mark.usefixtures("offline")
def test_r2_without_credentials_fails_cleanly(tmp_path, seed, capsys):
    assert run("seed-push", "--seed", seed, "--store", "r2") == 1
    assert "R2_ENDPOINT not set" in capsys.readouterr().err
