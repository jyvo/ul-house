"""CI entry point for pipeline stages."""
from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ul_house.build import publish as pub
from ul_house.build import release as rel
from ul_house.build import sign
from ul_house.build.store import Layout, ObjectMissing, StoreError, open_store, sha256_file, verify_object
from ul_house.seed.store import SCHEMA_VERSION as SEED_SCHEMA_VERSION

DESCRIPTION = (__doc__ or "CI entry point for pipeline stages.").split("\n\n")[0]
STAGES = ("pull", "crawl", "parse", "model", "gate", "release", "publish", "checkpoint", "all", "keygen", "seed-push")
ALL_ORDER = ("pull", "crawl", "parse", "model", "gate", "release", "publish")
EXIT_OK, EXIT_FAILED, EXIT_STATE_MISSING, EXIT_CHECK_FAILED, EXIT_USAGE = 0, 1, 2, 4, 64
REPO_ROOT = Path(__file__).resolve().parents[3]
CATALOG_REL = "src/ul_house/catalog.toml"
POLICY_REL = "analytics/gate_policy.toml"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="python -m ul_house.build", description=DESCRIPTION)
    p.add_argument("stage", choices=STAGES, metavar="stage", help="one of: " + ", ".join(STAGES))
    p.add_argument("--work", type=Path, default=Path("work"), help="work directory (default ./work)")
    p.add_argument("--env-file", default=None, metavar="PATH", help="KEY=VALUE file (default ./.env if present)")
    p.add_argument("--no-env-file", action="store_true", help="do not read an env file")
    p.add_argument("--store", default=None, help='object store: "local:PATH" or "r2" (default $UL_HOUSE_STORE, else local:./store)')
    crawl = p.add_argument_group("crawl (passed to ul_house.crawl.run)")
    crawl.add_argument("--limit", type=int, metavar="N", help="at most N detail-page requests")
    crawl.add_argument("--resume", action="store_true", help="continue the active run")
    crawl.add_argument("--base-url", help="the site root (tests only)")
    crawl.add_argument("--init", action="store_true", help="create the seed if it does not exist")
    crawl.add_argument("--catalog", type=Path, help="the scope file (default src/ul_house/catalog.toml)")
    gate = p.add_argument_group("gate")
    canary = gate.add_mutually_exclusive_group()
    canary.add_argument("--canary-failed", dest="canary", action="store_const", const=True, default=None,
                        help="the data workflow's canary step failed")
    canary.add_argument("--canary-passed", dest="canary", action="store_const", const=False,
                        help="the data workflow's canary step passed (neither flag: not run)")
    release = p.add_argument_group("release")
    release.add_argument("--force-snapshot", action="store_true", help="make this release snapshot-only")
    release.add_argument("--allow-dirty", action="store_true", help="publish a dirty build (local store only)")
    publish = p.add_argument_group("publish")
    publish.add_argument("--api-base", help="public API base URL for the post-publish check (default $UL_HOUSE_API_BASE)")
    publish.add_argument("--skip-check", action="store_true", help="skip the post-publish check (local store only)")
    publish.add_argument("--approve", type=int, metavar="N", help="publish held candidate N (skips every other stage)")
    publish.add_argument("--reason", help="why the candidate is approved (required with --approve)")
    publish.add_argument("--approver", help="who approves (default $GITHUB_ACTOR)")
    publish.add_argument("--no-retain", action="store_true", help="skip retention after publishing")
    keygen = p.add_argument_group("keygen")
    keygen.add_argument("--out", type=Path, help="directory for signing.pem and public.hex")
    push = p.add_argument_group("seed-push (manual, owner-run)")
    push.add_argument("--seed", type=Path, help="local crawl seed to upload")
    push.add_argument("--dry-run", action="store_true", help="report only; upload nothing")
    return p


def write_outcome(work: Path, outcome: str, **fields: Any) -> None:
    """write outcome file and output"""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "outcome.json").write_text(
        json.dumps({"outcome": outcome, **fields}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"outcome={outcome}\n")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _mb(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB" if size >= 100_000 else f"{size / 1000:.0f} KB"


def _line_crawl(work: Path) -> str | None:
    seed = Path(work) / "seed.sqlite"
    if not seed.exists():
        return None
    try:
        with sqlite3.connect(f"file:{seed.resolve()}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT run_id, started_at, ended_at, pages_fetched, pages_changed, pages_failed, pages_not_modified "
                "FROM crawl_run ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    run_id, started, ended, fetched, changed, failed, not_modified = row
    seconds = ""
    try:
        if ended:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            seconds = f" | {int((datetime.strptime(ended, fmt) - datetime.strptime(started, fmt)).total_seconds())} s"
    except ValueError:
        pass
    rate = f" | 304 rate {100 * not_modified // fetched} %" if fetched else ""
    return f"Crawl      run {started[:16]}Z{seconds} | fetched {fetched} | changed {changed} | failed {failed}{rate}"


def _line_gate(work: Path) -> str | None:
    gate = _load_json(Path(work) / "gate.json")
    if not gate:
        return None
    found = [f for f in gate.get("findings", []) if f.get("level") in ("warning", "blocker", "error")]
    detail = ", ".join(f"{f['check']} {f['scope']} {f.get('count', '')}".strip() for f in found[:3])
    more = f" (+{len(found) - 3} more)" if len(found) > 3 else ""
    word = "1 finding" if len(found) == 1 else f"{len(found)} findings"
    return f"Gate       {gate['level']} | {word}" + (f" ({detail}{more})" if found else "")


def _line_release(work: Path) -> str | None:
    decision = _load_json(Path(work) / "release" / "decision.json")
    if not decision:
        return None
    if decision["action"] == "heartbeat":
        return f"Release    no changes | revision {decision['revision']} stays current"
    n = decision["revision"]
    bundle = _load_json(Path(work) / "release" / str(n) / "bundle.json")
    manifest = None
    if bundle:
        manifest = _load_json(Path(work) / "release" / str(n) / "pub" / Layout.rel_manifest(n))
    if not manifest:
        return f"Release    revision {n} | {decision['reason']}"
    art = manifest["artifacts"]
    updates = art["updates"]
    newest = next((u for u in updates if u["from_revision"] == n - 1), None)
    parts = [f"revision {n}", f"snapshot {_mb(art['snapshot']['bytes'])}"]
    if newest:
        parts.append(f"update {n - 1}->{n} {_mb(newest['bytes'])}")
    parts.append("snapshot-only (" + ", ".join(manifest["snapshot_only_reasons"]) + ")" if manifest["snapshot_only"]
                 else f"{len(updates)} packages")
    added = sum(d["icon_count"] for d in art["icon_packs"]["deltas"] if d["from_revision"] == n - 1)
    parts.append(f"icons +{added}")
    return "Release    " + " | ".join(parts)


def _line_publish(work: Path) -> str | None:
    outcome = _load_json(Path(work) / "outcome.json")
    if not outcome:
        return None
    kind = outcome["outcome"]
    n = outcome.get("revision")
    if kind == "published":
        check = {"ok": "post-publish check ok", "skipped": "post-publish check skipped",
                 "failed": "POST-PUBLISH CHECK FAILED"}.get(outcome.get("check", "skipped"), "")
        text = f"published | current.json -> {n} | {check}"
        if outcome.get("approved_by"):
            text += f" | approved by {outcome['approved_by']}: {outcome.get('reason', '')}"
        return "Publish    " + text
    if kind == "held":
        return f"Publish    held | candidate {n} awaits approval (gate blocker)"
    if kind == "heartbeat":
        return f"Publish    heartbeat | revision {n} re-signed, no new data"
    return f"Publish    {kind}"


_LINES: dict[str, Callable[[Path], str | None]] = {
    "crawl": _line_crawl, "gate": _line_gate, "release": _line_release, "publish": _line_publish,
}


def render_summary(work: Path) -> str:
    """job summary lines for work directory"""
    lines = [line for fn in _LINES.values() if (line := fn(Path(work)))]
    return "\n".join(lines) + ("\n" if lines else "")


def _append_summary(stage: str, work: Path) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    fn = _LINES.get(stage)
    line = fn(Path(work)) if fn else None
    if target and line:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    if line:
        print(line)


# helpers
def _store(args: argparse.Namespace):
    return open_store(args.store or os.environ.get("UL_HOUSE_STORE") or "local:./store")


def _work(args: argparse.Namespace) -> Path:
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    return work


def _fail(message: str, code: int = EXIT_FAILED) -> int:
    print(message, file=sys.stderr)
    return code


def _git_line(*argv: str) -> str | None:
    return rel._git(REPO_ROOT, *argv)


def _revision(work: Path, store) -> tuple[int, bool]:
    marker = _load_json(Path(work) / "revision.json")
    if marker and isinstance(marker.get("revision"), int):
        return int(marker["revision"]), bool(marker.get("bootstrap", False))
    history = Path(work) / "history" / "current.duckdb"
    return rel.next_revision(store, history if history.exists() else None), not history.exists()


def _call_with(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """call fn with accepted keyword arguments"""
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})


def _event_trigger() -> str:
    return {"schedule": "schedule", "workflow_dispatch": "dispatch"}.get(os.environ.get("GITHUB_EVENT_NAME", ""), "manual")


# stages
def _stage_pull(args: argparse.Namespace) -> int:
    work, store = _work(args), _store(args)
    try:
        store.get_file(Layout.SEED_CURRENT, work / "seed.sqlite")
    except ObjectMissing:
        return _fail(f"seed missing: {Layout.SEED_CURRENT} is not in the store (create it with a first crawl --init)",
                     EXIT_STATE_MISSING)
    history = work / "history" / "current.duckdb"
    bootstrap = False
    try:
        store.get_file(Layout.HISTORY_CURRENT, history)
    except ObjectMissing:
        if store.head(Layout.CURRENT) is not None:
            return _fail(f"history missing: {Layout.HISTORY_CURRENT} is not in the store but a release is published",
                         EXIT_STATE_MISSING)
        bootstrap = True
    revision = rel.next_revision(store, history if history.exists() else None)
    (work / "revision.json").write_text(json.dumps({"revision": revision, "bootstrap": bootstrap}) + "\n", encoding="utf-8")
    print(f"pulled build state; next revision {revision}" + (" (bootstrap)" if bootstrap else ""))
    return EXIT_OK


def _stage_crawl(args: argparse.Namespace) -> int:
    work = _work(args)
    build = rel.BuildInfo.collect(REPO_ROOT)
    cmd = [sys.executable, "-m", "ul_house.crawl.run", "--seed", str(work / "seed.sqlite"),
           "--trigger", _event_trigger(), "--code-commit", build.build_commit]
    catalog_commit = _git_line("log", "-1", "--format=%H", "--", CATALOG_REL)
    if catalog_commit:
        cmd += ["--catalog-commit", catalog_commit]
    if args.limit is not None:
        cmd += ["--limit", str(args.limit)]
    if args.resume:
        cmd.append("--resume")
    if args.base_url:
        cmd += ["--base-url", args.base_url]
    if args.init:
        cmd.append("--init")
    if args.catalog:
        cmd += ["--catalog", str(args.catalog)]
    code = subprocess.run(cmd, check=False).returncode
    if code == 0:
        return EXIT_OK
    if code == 2:
        return _fail("crawl refused (scope fingerprint changed under --resume, or the seed is missing)", EXIT_STATE_MISSING)
    return _fail(f"crawl failed (exit {code})")


def _stage_parse(args: argparse.Namespace) -> int:
    work = _work(args)
    from ul_house.build import load
    try:
        load.load_seed(work / "seed.sqlite", work)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"parse failed: {type(exc).__name__}: {exc}")
    return EXIT_OK


def _stage_model(args: argparse.Namespace) -> int:
    work, store = _work(args), _store(args)
    from ul_house import settings
    from ul_house.build import dbt
    revision, bootstrap = _revision(work, store)
    catalog = Path(args.catalog) if args.catalog else settings.CATALOG_PATH
    build = rel.BuildInfo.collect(REPO_ROOT)
    try:
        run_id = _call_with(
            dbt.run_dbt, work, revision=revision, scope=settings.load_scope(catalog),
            policy_path=getattr(dbt, "POLICY_PATH", REPO_ROOT / POLICY_REL), bootstrap=bootstrap,
            code_commit=build.build_commit,
            catalog_commit=_git_line("log", "-1", "--format=%H", "--", CATALOG_REL) or "unknown",
            policy_commit=_git_line("log", "-1", "--format=%H", "--", POLICY_REL) or "unknown",
            dirty=build.dirty)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"model failed: {type(exc).__name__}: {exc}")
    print(f"transform run {run_id} for revision {revision}")
    return EXIT_OK


def _stage_gate(args: argparse.Namespace) -> int:
    work = _work(args)
    from ul_house.build import gate
    try:
        decision = gate.decide(work, getattr(gate, "POLICY_PATH", REPO_ROOT / POLICY_REL),
                               canary_failed=args.canary)
        writer = getattr(gate, "write", None)
        if callable(writer):
            try:
                writer(decision, work)                     # gate file and rows
            except Exception as exc:                       # already recorded; keep file  # noqa: BLE001
                print(f"gate: obs rows not written ({type(exc).__name__}: {exc})", file=sys.stderr)
        (work / "gate.json").write_text(
            json.dumps(decision.to_mapping(), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
            encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"gate failed: {type(exc).__name__}: {exc}")
    _append_summary("gate", work)
    if decision.level == "error":
        write_outcome(work, "gate_error", revision=decision.revision)
        return _fail("gate level is error: no release")
    return EXIT_OK


def _stage_release(args: argparse.Namespace) -> int:
    work, store = _work(args), _store(args)
    revision, _ = _revision(work, store)
    build = rel.BuildInfo.collect(REPO_ROOT)
    inputs = rel.ReleaseInputs(work, revision, store, build, force_snapshot=bool(args.force_snapshot))
    try:
        decision = rel.build_release(inputs)
    except (rel.ReleaseError, StoreError) as exc:
        return _fail(f"release failed: {exc}")
    print(f"release decision: {decision.action} ({decision.reason})")
    _append_summary("release", work)
    return EXIT_OK


def _post_check(args: argparse.Namespace, store, work: Path, current_doc: dict[str, Any], trusted: Sequence[str]) -> str:
    """run post-publish check"""
    if args.skip_check:
        return "skipped"
    base = _api_base(args)
    pub.post_publish_check(base, trusted_keys=trusted, expected_revision=current_doc["revision"],
                           current_bytes_expected=store.get(Layout.CURRENT))
    return "ok"


def _api_base(args: argparse.Namespace) -> str | None:
    return args.api_base or os.environ.get("UL_HOUSE_API_BASE")


def _trusted(args: argparse.Namespace, store) -> tuple[str, ...]:
    """keys the release must verify under"""
    base = _api_base(args)
    if os.environ.get(sign.ENV_DEV_PUBLIC_KEYS) and sign.dev_public_keys(store.kind, base) is None:
        print(f"{sign.ENV_DEV_PUBLIC_KEYS} is ignored: not a local store with a loopback API base",
              file=sys.stderr)
    return sign.trusted_public_keys(store.kind, api_base=base)


def _retain(args: argparse.Namespace, store) -> None:
    if args.no_retain:
        return
    report = pub.retain(store)
    print(f"retention: kept {len(report.kept)}, deleted {len(report.deleted)}, unrecognised {len(report.unknown)}")


def _require_api_base(args: argparse.Namespace) -> int | None:
    if args.skip_check or args.api_base or os.environ.get("UL_HOUSE_API_BASE"):
        return None
    return _fail("--api-base (or $UL_HOUSE_API_BASE) is required for the post-publish check; "
                 "use --skip-check on a local store", EXIT_USAGE)


def _require_clean_commit(bundle: rel.Bundle, store) -> None:
    if store.kind == "local":
        return
    commit = bundle.manifest()["build"]["build_commit"]
    if bundle.dirty:
        raise pub.PublishError(f"revision {bundle.revision} was built from a dirty tree: not publishable to {store.kind}")
    if not re.fullmatch(r"[0-9a-f]{40}", str(commit)) or set(commit) == {"0"}:
        raise pub.PublishError(f"revision {bundle.revision} has no clean build commit ({commit!r}): "
                               f"not publishable to {store.kind}")


def _stage_publish(args: argparse.Namespace) -> int:
    work, store = _work(args), _store(args)
    try:
        if args.approve is not None:
            return _require_api_base(args) or _publish_approval(args, store, work)
        decision = rel.ReleaseDecision.load(work)
        if decision.action == "hold":
            bundle = rel.Bundle.load(decision.bundle_dir)
            _require_clean_commit(bundle, store)
            pub.hold(bundle, store)
            write_outcome(work, "held", revision=decision.revision)
            _append_summary("publish", work)
            return EXIT_OK
        if decision.action == "publish":
            _require_clean_commit(rel.Bundle.load(decision.bundle_dir), store)
            if (refused := _require_api_base(args)) is not None:
                return refused
        signer = sign.Signer.from_env()
        trusted = _trusted(args, store)
        if decision.action == "heartbeat":
            state = pub.BuildState(work / "seed.sqlite", work / "history" / "current.duckdb")
            fresh = pub.heartbeat(store, signer=signer, trusted_keys=trusted, build_state=state)
            write_outcome(work, "heartbeat", revision=fresh["revision"])
            _append_summary("publish", work)
            return EXIT_OK
        bundle = rel.Bundle.load(decision.bundle_dir)
        result = pub.publish(bundle, store, signer=signer, trusted_keys=trusted, allow_dirty=bool(args.allow_dirty),
                             work_dir=work)
        return _after_publish(args, store, work, result, trusted)
    except (pub.PublishError, rel.ReleaseError, sign.SigningKeyError, StoreError) as exc:
        return _fail(f"publish failed: {type(exc).__name__}: {exc}")


def _publish_approval(args: argparse.Namespace, store, work: Path) -> int:
    if not args.reason:
        return _fail("--reason is required with --approve", EXIT_USAGE)
    approver = args.approver or os.environ.get("GITHUB_ACTOR") or "unknown"
    signer = sign.Signer.from_env()
    trusted = _trusted(args, store)
    result = pub.approve(args.approve, store, reason=args.reason, approver=approver,
                         run_id=os.environ.get("GITHUB_RUN_ID") or "local", work_dir=work, signer=signer,
                         trusted_keys=trusted, allow_dirty=bool(args.allow_dirty))
    return _after_publish(args, store, work, result, trusted, approved_by=approver, reason=args.reason)


def _after_publish(args: argparse.Namespace, store, work: Path, result: pub.PublishResult, trusted: Sequence[str],
                   **extra: Any) -> int:
    check = "ok"
    code = EXIT_OK
    try:
        check = _post_check(args, store, work, result.current, trusted)
    except pub.PostPublishCheckFailed as exc:
        print(f"post-publish check failed: {exc}", file=sys.stderr)
        check, code = "failed", EXIT_CHECK_FAILED
    write_outcome(work, "published", revision=result.revision, check=check, **extra)
    _append_summary("publish", work)
    if code == EXIT_OK:
        _retain(args, store)
    return code


def _stage_checkpoint(args: argparse.Namespace) -> int:
    from ul_house.build import checkpoint
    work, store = _work(args), _store(args)
    try:
        result = checkpoint.run_checkpoint(store, work)
    except (checkpoint.CheckpointError, StoreError) as exc:
        return _fail(f"checkpoint failed: {exc}")
    archived = sum(c["archived"] for c in result.counts.values())
    print(f"checkpoint {result.checkpoint_id}: archived {archived} rows"
          + (f" into {result.archive_name}" if result.archive_name else ""))
    return EXIT_OK


def _stage_keygen(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else Path("keys")
    pem_path, hex_path = out / "signing.pem", out / "public.hex"
    out.mkdir(parents=True, exist_ok=True)
    if pem_path.exists():
        return _fail(f"{pem_path} already exists; refusing to overwrite a signing key", EXIT_USAGE)
    pem, public = sign.generate_keypair()
    fd = os.open(pem_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    hex_path.write_text(public + "\n", encoding="utf-8")
    print(public)                                          # private key never printed
    print(f"wrote {pem_path} (the RELEASE_SIGNING_KEY secret) and {hex_path}", file=sys.stderr)
    return EXIT_OK


SEED_CONTENT_TYPE = "application/vnd.sqlite3"


def _seed_problem(seed: Path) -> tuple[str, int] | None:
    """return refusal for an unfit seed"""
    if not seed.is_file():
        return f"seed not found: {seed}", EXIT_STATE_MISSING
    try:
        with sqlite3.connect(f"file:{seed.resolve()}?mode=ro", uri=True) as conn:
            check = conn.execute("PRAGMA integrity_check").fetchall()
            if check != [("ok",)]:
                return "seed failed integrity_check", EXIT_USAGE
            row = conn.execute("SELECT value FROM meta WHERE key = 'seed.schema_version'").fetchone()
            if row is None or row[0] != str(SEED_SCHEMA_VERSION):
                return (f"seed schema {row[0] if row else 'missing'}, this code needs {SEED_SCHEMA_VERSION}",
                        EXIT_USAGE)
            if conn.execute("SELECT 1 FROM frontier WHERE state = 'in_flight' LIMIT 1").fetchone():
                return "seed has in_flight frontier rows: resume the crawl first", EXIT_USAGE
            if not conn.execute("SELECT 1 FROM crawl_run WHERE status = 'complete' LIMIT 1").fetchone():
                return "seed has no complete crawl run", EXIT_USAGE
    except sqlite3.Error as exc:
        return f"seed unreadable: {type(exc).__name__}: {exc}", EXIT_USAGE
    return None


def _fold_copy(seed: Path, dest: Path) -> None:
    """single-file copy, WAL folded in"""
    src = sqlite3.connect(f"file:{seed.resolve()}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        out.execute("PRAGMA journal_mode=DELETE")
    finally:
        out.close()
        src.close()


def _stage_seed_push(args: argparse.Namespace) -> int:
    if args.seed is None:
        return _fail("seed-push requires --seed PATH", EXIT_USAGE)
    seed = Path(args.seed)
    if (problem := _seed_problem(seed)) is not None:
        return _fail(f"seed-push refused: {problem[0]}", problem[1])
    store = _store(args)
    where = store.kind + (f" bucket {store.bucket}" if hasattr(store, "bucket") else "")
    with tempfile.TemporaryDirectory(prefix="seed-push-") as tmp:
        copy = Path(tmp) / "seed.sqlite"
        _fold_copy(seed, copy)
        sha, size = sha256_file(copy), copy.stat().st_size
        try:
            existing = store.head(Layout.SEED_CURRENT)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"seed-push failed: {type(exc).__name__}: {exc}")
        if existing is not None:
            same = "unknown" if existing.sha256 is None else ("no" if existing.sha256 != sha else "yes")
            print(f"store holds {Layout.SEED_CURRENT}: {existing.size} bytes, sha256 {existing.sha256 or 'unknown'}; "
                  f"identical to local: {same}")
        plan = f"{Layout.SEED_CURRENT}: {size} bytes, sha256 {sha}, store {where}"
        if existing is not None and existing.sha256 == sha:
            print(f"store already has this seed, nothing to upload: {plan}")
            return EXIT_OK
        if args.dry_run:
            print(f"dry run, would upload {plan}")
            return EXIT_OK
        try:
            store.put_file(Layout.SEED_CURRENT, copy, content_type=SEED_CONTENT_TYPE)
            verify_object(store, Layout.SEED_CURRENT, sha)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"seed-push failed: {type(exc).__name__}: {exc}")
    print(f"uploaded and verified {plan}")
    return EXIT_OK


def _stage_all(args: argparse.Namespace) -> int:
    for name in ALL_ORDER:
        code = globals()[f"_stage_{name}"](args)           # looked up at call time
        if code != EXIT_OK:
            return code
    return EXIT_OK


def parse_env_file(text: str) -> dict[str, str]:
    """parse KEY=VALUE lines"""
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith(("export ", "export\t")):
            line = line[len("export"):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        quote = value[:1]
        if quote in ("'", '"') and (len(value) < 2 or value[-1] != quote):
            for j in range(i, len(lines)):                 # multi-line quoted value
                if lines[j].rstrip().endswith(quote):
                    value = "\n".join([value, *(ln.rstrip() for ln in lines[i:j + 1])])
                    i = j + 1
                    break
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def load_env_file(args: argparse.Namespace) -> int | None:
    if args.no_env_file or args.env_file == "":
        return None
    explicit = args.env_file is not None
    path = Path(args.env_file) if explicit else Path(".env")
    if not path.is_file():
        return _fail(f"env file not found: {path}", EXIT_USAGE) if explicit else None
    try:
        values = parse_env_file(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(f"env file unreadable: {path}: {type(exc).__name__}", EXIT_USAGE)
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (refused := load_env_file(args)) is not None:
        return refused
    store_uri = args.store or os.environ.get("UL_HOUSE_STORE") or "local:./store"
    local = store_uri.startswith("local:")
    if args.allow_dirty and not local:
        return _fail("--allow-dirty is only allowed with a local store", EXIT_USAGE)
    if args.skip_check and not local:
        return _fail("--skip-check is only allowed with a local store", EXIT_USAGE)
    try:
        return globals()[f"_stage_{args.stage.replace('-', '_')}"](args)
    except (rel.ReleaseError, StoreError, sign.SigningKeyError, ValueError) as exc:
        return _fail(f"{args.stage} failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
