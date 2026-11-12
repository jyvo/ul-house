from __future__ import annotations

import json
import re
import shutil
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from ul_house.build import release as rel
from ul_house.build import sign
from ul_house.build.release import Bundle, utc_now, utc_text
from ul_house.build.store import (
    ImmutableConflict,
    Layout,
    ObjectMissing,
    ObjectStore,
    StoreError,
    put_immutable,
    sha256_bytes,
    sha256_file,
    verify_object,
)

EXPIRY = timedelta(days=14)                 # release expiry
CANDIDATE_DAYS = 14                         # candidate retention
BACKUP_DAYS = 14                            # backup retention
WEEKLY_WEEKS = 13                           # weekly snapshots, three months
USER_AGENT = "ul-house-publish-check"


class PublishError(Exception):
    step: int

    def __init__(self, message: str = "", *, step: int = 0):
        super().__init__(message)
        self.step = step


class RevisionConflict(PublishError):
    """Bundle revision not newer."""


class DirtyBuild(PublishError):
    """Dirty bundle refused."""


class CandidateMissing(PublishError):
    pass


class CandidateStale(PublishError):
    """Current moved past candidate."""


class PostPublishCheckFailed(PublishError):
    pass


class ImmutableAtStep(PublishError, ImmutableConflict):
    """Immutable object exists with other bytes."""


@dataclass(frozen=True)
class BuildState:
    seed: Path
    history: Path


@dataclass(frozen=True)
class Approval:
    revision: int
    reason: str
    approver: str
    run_id: str                  # approval run
    approved_at: str
    candidate_run_id: str = ""   # candidate-holding run


@dataclass(frozen=True)
class PublishResult:
    revision: int
    current: dict[str, Any]
    uploaded: tuple[str, ...]
    reused: tuple[str, ...]
    published_at: str


@dataclass(frozen=True)
class CheckReport:
    ok: bool
    attempts: int
    steps: tuple[str, ...]


@dataclass(frozen=True)
class RetentionReport:
    kept: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    dry_run: bool = False


def _wrap(step: int, exc: BaseException) -> PublishError:
    if isinstance(exc, PublishError):
        return exc
    if isinstance(exc, ImmutableConflict):
        return ImmutableAtStep(str(exc), step=step)
    return PublishError(f"{type(exc).__name__}: {exc}", step=step)


# current json
def current_bytes(current: Mapping[str, Any]) -> bytes:
    """stored encoding including sig"""
    return json.dumps(dict(current), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def make_current(manifest: Mapping[str, Any], manifest_sha256: str, *, published_at: str, checked_at: str) -> dict[str, Any]:
    checked = datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return {
        "revision": manifest["revision"],
        "dataset_schema_version": manifest["dataset_schema_version"],
        "api_schema_version": manifest["api_schema_version"],
        "minimum_revision": manifest["minimum_revision"],
        "manifest": Layout.rel_manifest(manifest["revision"]),
        "manifest_sha256": manifest_sha256,
        "published_at": published_at,
        "checked_at": checked_at,
        "expires_at": utc_text(checked + EXPIRY),
    }


def read_current(store: ObjectStore) -> dict[str, Any] | None:
    try:
        return json.loads(store.get(Layout.CURRENT))
    except ObjectMissing:
        return None


def _check_current(current: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    n = current["revision"]
    if current["manifest"] != Layout.rel_manifest(n):
        raise rel.ContractError("INV-1: current.manifest is not manifests/{revision}.json")
    if current["minimum_revision"] > n:
        raise rel.ContractError("INV-2: minimum_revision > revision")
    for key in ("revision", "minimum_revision", "dataset_schema_version", "api_schema_version"):
        if manifest[key] != current[key]:
            raise rel.ContractError(f"INV-2: manifest {key} != current {key}")
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    published, checked, expires = (datetime.strptime(current[k], fmt) for k in ("published_at", "checked_at", "expires_at"))
    if not published <= checked < expires or expires != checked + EXPIRY:
        raise rel.ContractError("INV-3: published_at <= checked_at < expires_at = checked_at + 14 days violated")


# duckdb history tables
RELEASE_DDL = """
CREATE TABLE IF NOT EXISTS release (
  revision          BIGINT  PRIMARY KEY,
  transform_run_id  VARCHAR NOT NULL,
  crawl_run_id      VARCHAR,
  seed_sha256       VARCHAR NOT NULL,
  snapshot_sha256   VARCHAR NOT NULL,
  package_shas      VARCHAR NOT NULL,
  icon_pack_shas    VARCHAR NOT NULL,
  manifest_sha256   VARCHAR NOT NULL,
  signature         VARCHAR NOT NULL,
  published_at      VARCHAR NOT NULL,
  minimum_revision  BIGINT  NOT NULL,
  snapshot_only     BOOLEAN NOT NULL,
  gate_level        VARCHAR NOT NULL,
  run_id            VARCHAR NOT NULL)
"""
APPROVAL_DDL = """
CREATE TABLE IF NOT EXISTS obs_approval (
  revision          BIGINT  PRIMARY KEY,
  candidate_run_id  VARCHAR NOT NULL,
  approved_by       VARCHAR NOT NULL,
  reason            VARCHAR NOT NULL,
  approved_at       VARCHAR NOT NULL,
  approval_run_id   VARCHAR NOT NULL,
  observed_at       VARCHAR NOT NULL)
"""
RELEASE_COLUMNS = ("revision", "transform_run_id", "crawl_run_id", "seed_sha256", "snapshot_sha256",
                   "package_shas", "icon_pack_shas", "manifest_sha256", "signature", "published_at",
                   "minimum_revision", "snapshot_only", "gate_level", "run_id")


def record_release(history: Path, row: Mapping[str, Any]) -> None:
    """insert release row, replacing same revision"""
    missing = [c for c in RELEASE_COLUMNS if c not in row]
    if missing:
        raise PublishError(f"release row lacks {missing}")
    conn = duckdb.connect(str(history))
    try:
        conn.execute(RELEASE_DDL)
        conn.execute("DELETE FROM release WHERE revision = ?", [row["revision"]])
        conn.execute(f"INSERT INTO release ({', '.join(RELEASE_COLUMNS)}) VALUES ({', '.join('?' * len(RELEASE_COLUMNS))})",
                     [row[c] for c in RELEASE_COLUMNS])
    finally:
        conn.close()


def record_approval(history: Path, a: Approval) -> None:
    conn = duckdb.connect(str(history))
    try:
        conn.execute(APPROVAL_DDL)
        conn.execute("DELETE FROM obs_approval WHERE revision = ?", [a.revision])
        conn.execute("INSERT INTO obs_approval VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [a.revision, a.candidate_run_id, a.approver, a.reason, a.approved_at, a.run_id, a.approved_at])
    finally:
        conn.close()


# pub
_ORDER = (("pub/icons/packs/", 1), ("pub/icons/", 0), ("pub/snapshots/", 2), ("pub/updates/", 3),
          ("pub/lineage/", 4), ("pub/manifests/", 5))


def _upload_rank(path: str) -> int:
    for prefix, rank in _ORDER:
        if path.startswith(prefix):
            return rank
    return 9


def _bundle_manifest_refs(manifest: Mapping[str, Any]) -> dict[str, str]:
    """artifact path to sha256"""
    art = manifest["artifacts"]
    refs = {art["snapshot"]["path"]: art["snapshot"]["sha256"], art["lineage"]["path"]: art["lineage"]["sha256"],
            art["icon_packs"]["base"]["path"]: art["icon_packs"]["base"]["sha256"]}
    for u in art["updates"]:
        refs[u["path"]] = u["sha256"]
    for d in art["icon_packs"]["deltas"]:
        refs[d["path"]] = d["sha256"]
    return refs


def publish(bundle: Bundle, store: ObjectStore, *, signer: sign.Signer,
            trusted_keys: Sequence[str] | None = None, clock: Callable[[], datetime] = utc_now,
            allow_dirty: bool = False, contract_dir: Path | None = rel.DEFAULT_CONTRACT_DIR,
            approval: Approval | None = None, work_dir: Path) -> PublishResult:
    n = bundle.revision
    work = Path(work_dir)

    # preconditions
    try:
        bundle = Bundle.load(bundle.root)               # re-hash every file
    except Exception as exc:
        raise _wrap(0, exc) from exc
    if bundle.dirty and (store.kind != "local" or not allow_dirty):
        raise DirtyBuild(f"revision {n} was built from a dirty tree"
                         + ("" if store.kind == "local" else " and the store is not local"), step=0)
    try:
        current_now = read_current(store)
    except Exception as exc:
        raise _wrap(0, exc) from exc
    if current_now is not None and n <= current_now["revision"]:
        raise RevisionConflict(f"revision {n} <= current {current_now['revision']}: clients never downgrade", step=0)
    manifest = bundle.manifest()
    if manifest["revision"] != n or sha256_file(bundle.root / "pub" / Layout.rel_manifest(n)) != bundle.manifest_sha256:
        raise PublishError("bundle manifest does not match bundle.json", step=0)

    uploaded: list[str] = []
    reused: list[str] = []

    # upload immutable objects, manifest last
    step = 1
    try:
        for f in sorted(bundle.pub_files(), key=lambda f: (_upload_rank(f.path), f.path)):
            info, wrote = put_immutable(store, f.path, bundle.root / f.path, content_type=f.content_type)
            (uploaded if wrote else reused).append(f.path)
    except Exception as exc:
        raise _wrap(step, exc) from exc

    # read back and verify objects
    step = 2
    try:
        for f in bundle.pub_files():
            verify_object(store, f.path, f.sha256)
        lineage = rel.read_json_gz(bundle.root / "pub" / manifest["artifacts"]["lineage"]["path"])
        present = {i.key for i in store.list(Layout.pub("icons/"))}
        absent = [i for i in lineage["icons"] if Layout.pub(Layout.rel_icon(i["kind"], i["sha256"])) not in present]
        if absent:
            raise PublishError(f"{len(absent)} icon(s) of the snapshot are not in the store, e.g. {absent[0]}")
    except Exception as exc:
        raise _wrap(step, exc) from exc

    # sign and verify signature
    step = 3
    try:
        now = utc_text(clock())
        keys = tuple(trusted_keys) if trusted_keys is not None else sign.trusted_public_keys(store.kind)
        current = signer.sign(make_current(manifest, bundle.manifest_sha256, published_at=now, checked_at=now))
        sign.verify(current, keys)
        rel.validate("current", current, contract_dir)
        _check_current(current, manifest)
    except Exception as exc:
        raise _wrap(step, exc) from exc

    # replay against stored objects
    step = 4
    try:
        _replay_stored(bundle, manifest, store, work / "publish" / str(n) / "replay")
    except Exception as exc:
        raise _wrap(step, exc) from exc

    # upload build state
    step = 5
    try:
        _upload_build_state(bundle, manifest, store, current, work / "publish" / str(n), approval)
    except Exception as exc:
        raise _wrap(step, exc) from exc

    # commit point
    step = 6
    try:
        data = current_bytes(current)
        store.put(Layout.CURRENT, data, content_type="application/json")
        if store.get(Layout.CURRENT) != data:
            raise PublishError("pub/current.json read back differently")
    except Exception as exc:
        raise _wrap(step, exc) from exc

    _cleanup_candidates(store, n)                       # best effort cleanup
    shutil.rmtree(work / "publish" / str(n), ignore_errors=True)
    return PublishResult(n, current, tuple(uploaded), tuple(reused), current["published_at"])


def _replay_stored(bundle: Bundle, manifest: Mapping[str, Any], store: ObjectStore, tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    art = manifest["artifacts"]
    n = bundle.revision
    snap_gz = tmp / f"unison-{n}.sqlite.gz"
    store.get_file(Layout.pub(art["snapshot"]["path"]), snap_gz, sha256=art["snapshot"]["sha256"])
    snap = tmp / f"unison-{n}.sqlite"
    rel.gunzip_file(snap_gz, snap)
    got = rel.table_hashes(snap, list(bundle.table_hashes))
    differing = [t for t in bundle.table_hashes if got[t] != bundle.table_hashes[t]]
    if differing:
        raise rel.ReplayError(f"stored snapshot {n} differs from the built one in: {', '.join(differing)}")
    updates = {u["from_revision"]: u for u in art["updates"]}
    for r in bundle.replay_from:
        u = updates[r]
        package = json.loads(_gunzip(store.get(Layout.pub(u["path"]), sha256=u["sha256"])))
        older = json.loads(store.get(Layout.pub(Layout.rel_manifest(r))))["artifacts"]["snapshot"]
        r_gz = tmp / f"unison-{r}.sqlite.gz"
        store.get_file(Layout.pub(older["path"]), r_gz, sha256=older["sha256"])
        rel.replay(r_gz, package, bundle.table_hashes, tmp_dir=tmp)
        r_gz.unlink(missing_ok=True)


def _gunzip(data: bytes) -> bytes:
    import gzip
    return gzip.decompress(data)


def _release_row(manifest: Mapping[str, Any], current: Mapping[str, Any], bundle: Bundle) -> dict[str, Any]:
    art = manifest["artifacts"]
    return {
        "revision": manifest["revision"],
        "transform_run_id": manifest["build"]["transform_run_id"],
        "crawl_run_id": manifest["build"]["crawl_run_id"],
        "seed_sha256": manifest["build"]["seed_sha256"],
        "snapshot_sha256": art["snapshot"]["sha256"],
        "package_shas": json.dumps({str(u["from_revision"]): u["sha256"] for u in art["updates"]},
                                   sort_keys=True, separators=(",", ":")),
        "icon_pack_shas": json.dumps({"base": art["icon_packs"]["base"]["sha256"],
                                      **{str(d["from_revision"]): d["sha256"] for d in art["icon_packs"]["deltas"]}},
                                     sort_keys=True, separators=(",", ":")),
        "manifest_sha256": bundle.manifest_sha256,
        "signature": current["sig"],
        "published_at": current["published_at"],
        "minimum_revision": manifest["minimum_revision"],
        "snapshot_only": bool(manifest["snapshot_only"]),
        "gate_level": manifest["gate"]["level"],
        "run_id": bundle.run_id,
    }


def _put_mutable_verified(store: ObjectStore, key: str, path: Path, content_type: str) -> None:
    info = store.put_file(key, path, content_type=content_type)
    verify_object(store, key, info.sha256 or sha256_file(path))


def _upload_build_state(bundle: Bundle, manifest: Mapping[str, Any], store: ObjectStore, current: Mapping[str, Any],
                        tmp: Path, approval: Approval | None) -> None:
    n = bundle.revision
    tmp.mkdir(parents=True, exist_ok=True)
    history = tmp / "history.duckdb"
    shutil.copy2(bundle.root / "build" / "history.duckdb", history)     # bundle files stay untouched
    record_release(history, _release_row(manifest, current, bundle))
    if approval is not None:
        record_approval(history, approval if approval.candidate_run_id else
                        Approval(approval.revision, approval.reason, approval.approver, approval.run_id,
                                 approval.approved_at, bundle.run_id))
    seed = bundle.root / "build" / "seed.sqlite"
    for key, path, ctype in ((Layout.seed_at(n), seed, "application/vnd.sqlite3"),
                             (Layout.history_at(n), history, "application/octet-stream")):
        try:
            put_immutable(store, key, path, content_type=ctype)
        except ImmutableConflict:
            # replace partial earlier attempt
            store.put_file(key, path, content_type=ctype)
        verify_object(store, key, sha256_file(path))
    _put_mutable_verified(store, Layout.SEED_CURRENT, seed, "application/vnd.sqlite3")
    _put_mutable_verified(store, Layout.HISTORY_CURRENT, history, "application/octet-stream")


def _cleanup_candidates(store: ObjectStore, upto: int) -> None:
    try:
        for info in store.list(Layout.CANDIDATES):
            parsed = Layout.parse_candidate_key(info.key)
            if parsed is not None and parsed[0] <= upto:
                store.delete(info.key)
    except StoreError:
        pass


def hold(bundle: Bundle, store: ObjectStore) -> str:
    """stored bundle bytes under candidates/N/"""
    n = bundle.revision
    bundle = Bundle.load(bundle.root)
    for info in store.list(Layout.CANDIDATES):
        parsed = Layout.parse_candidate_key(info.key)
        if parsed is not None and parsed[0] <= n:
            store.delete(info.key)
    prefix = Layout.candidate(n)
    for f in bundle.files:
        _put_mutable_verified(store, prefix + f.path, bundle.root / f.path, f.content_type)
    _put_mutable_verified(store, prefix + "bundle.json", bundle.root / "bundle.json", "application/json")
    return prefix


def fetch_candidate(revision: int, store: ObjectStore, dest: Path) -> Bundle:
    prefix = Layout.candidate(revision)
    try:
        meta = json.loads(store.get(prefix + "bundle.json"))
    except ObjectMissing:
        raise CandidateMissing(f"no held candidate for revision {revision}", step=0) from None
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for f in meta["files"]:
        store.get_file(prefix + f["path"], dest / f["path"], sha256=f["sha256"])
    (dest / "bundle.json").write_bytes(store.get(prefix + "bundle.json"))
    return Bundle.load(dest)


def approve(revision: int, store: ObjectStore, *, reason: str, approver: str, run_id: str, work_dir: Path,
            signer: sign.Signer, trusted_keys: Sequence[str] | None = None,
            clock: Callable[[], datetime] = utc_now,
            contract_dir: Path | None = rel.DEFAULT_CONTRACT_DIR, allow_dirty: bool = False) -> PublishResult:
    """publish a held candidate"""
    current_now = read_current(store)
    if current_now is not None and current_now["revision"] >= revision:
        raise CandidateStale(f"current is already revision {current_now['revision']}", step=0)
    bundle = fetch_candidate(revision, store, Path(work_dir) / "candidate" / str(revision))
    # stale if another run used revision
    for info in store.list(Layout.pub("manifests/")):
        n = Layout.parse_manifest_key(info.key)
        if n is None:
            continue
        if n > revision:
            raise CandidateStale(f"revision {n} exists in the store: candidate {revision} is out of date", step=0)
        if n == revision:
            stored = info.sha256 or sha256_bytes(store.get(info.key))
            if stored != bundle.manifest_sha256:
                raise CandidateStale(f"revision {revision} was already used by another run", step=0)
    a = Approval(revision, reason, approver, run_id, utc_text(clock()), bundle.run_id)
    return publish(bundle, store, signer=signer, trusted_keys=trusted_keys, clock=clock, allow_dirty=allow_dirty,
                   contract_dir=contract_dir, approval=a, work_dir=work_dir)


def heartbeat(store: ObjectStore, *, signer: sign.Signer, trusted_keys: Sequence[str] | None = None,
              clock: Callable[[], datetime] = utc_now, build_state: BuildState | None = None) -> dict[str, Any]:
    """refresh and re-sign unchanged release"""
    current = read_current(store)
    if current is None:
        raise PublishError("no pub/current.json to refresh", step=0)
    try:
        keys = tuple(trusted_keys) if trusted_keys is not None else sign.trusted_public_keys(store.kind)
        sign.verify(current, keys)
        now = utc_text(clock())
        fresh = {k: current[k] for k in ("revision", "dataset_schema_version", "api_schema_version", "minimum_revision",
                                         "manifest", "manifest_sha256", "published_at")}
        fresh["checked_at"] = now
        fresh["expires_at"] = utc_text(datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) + EXPIRY)
        fresh = signer.sign(fresh)
        sign.verify(fresh, keys)
        if build_state is not None:
            _put_mutable_verified(store, Layout.SEED_CURRENT, build_state.seed, "application/vnd.sqlite3")
            _put_mutable_verified(store, Layout.HISTORY_CURRENT, build_state.history, "application/octet-stream")
        data = current_bytes(fresh)
        store.put(Layout.CURRENT, data, content_type="application/json")
        if store.get(Layout.CURRENT) != data:
            raise PublishError("pub/current.json read back differently")
    except PublishError:
        raise
    except Exception as exc:
        raise PublishError(f"{type(exc).__name__}: {exc}", step=0) from exc
    return fresh


def urllib_fetch(url: str, timeout: float = 30.0) -> bytes:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")):
        raise PublishError(f"refusing to fetch {url!r}: HTTPS is required", step=7)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def post_publish_check(api_base: str, *, trusted_keys: Sequence[str], expected_revision: int,
                       fetch: Callable[[str], bytes] = urllib_fetch, attempts: int = 13, delay: float = 30.0,
                       sleep: Callable[[float], None] = time.sleep,
                       current_bytes_expected: bytes | None = None) -> CheckReport:
    """verify published release via public API"""
    base = api_base.rstrip("/")
    state: dict[str, Any] = {}

    def digest(url: str, want: str) -> None:
        got = sha256_bytes(fetch(url))
        if got != want:
            raise PostPublishCheckFailed(f"{url}: sha256 {got} != {want}", step=7)

    def version() -> None:
        body = fetch(f"{base}/v1/version")
        doc = json.loads(body)
        sign.verify(doc, trusted_keys)
        if doc.get("revision") != expected_revision:
            raise PostPublishCheckFailed(f"/v1/version serves revision {doc.get('revision')}, expected {expected_revision}", step=7)
        if current_bytes_expected is not None and body != current_bytes_expected:
            raise PostPublishCheckFailed("/v1/version differs from the stored pub/current.json", step=7)
        state["current"] = doc

    def manifest() -> None:
        body = fetch(f"{base}/v1/manifests/{expected_revision}")
        if sha256_bytes(body) != state["current"]["manifest_sha256"]:
            raise PostPublishCheckFailed("manifest sha256 differs from current.json", step=7)
        state["manifest"] = json.loads(body)

    def snapshot() -> None:
        digest(f"{base}/v1/snapshots/{expected_revision}", state["manifest"]["artifacts"]["snapshot"]["sha256"])

    def update() -> None:
        prev = expected_revision - 1
        entry = next((u for u in state["manifest"]["artifacts"]["updates"] if u["from_revision"] == prev), None)
        if entry is not None:
            digest(f"{base}/v1/updates/{prev}", entry["sha256"])

    def delta() -> None:
        prev = expected_revision - 1
        entry = next((d for d in state["manifest"]["artifacts"]["icon_packs"]["deltas"] if d["from_revision"] == prev), None)
        if entry is not None:
            name = entry["path"].rsplit("/", 1)[-1]
            digest(f"{base}/v1/icons/packs/{name}", entry["sha256"])

    steps = (("version", version), ("manifest", manifest), ("snapshot", snapshot), ("update", update),
             ("delta", delta))
    index, used, tries, last = 0, 0, 0, ""
    while index < len(steps):
        name, fn = steps[index]
        used += 1
        tries += 1
        try:
            fn()
            index += 1
            tries = 0
            continue
        except Exception as exc:                                    # any failure is retried  # noqa: BLE001
            last = f"{name}: {type(exc).__name__}: {exc}"
        if tries >= attempts:
            raise PostPublishCheckFailed(f"post-publish check failed after {tries} attempt(s): {last}", step=7)
        sleep(delay)
    return CheckReport(True, used, tuple(name for name, _ in steps))


def _parse_z(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


_BACKUP_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


def retain(store: ObjectStore, *, clock: Callable[[], datetime] = utc_now, dry_run: bool = False) -> RetentionReport:
    current = read_current(store)
    if current is None:
        return RetentionReport(dry_run=dry_run)
    n, m = current["revision"], current["minimum_revision"]
    now = clock()
    infos = store.list("")
    keys = [i.key for i in infos]

    snapshots = {r for k in keys if (r := Layout.parse_snapshot_key(k)) is not None}
    lineages = {r for k in keys if (r := Layout.parse_lineage_key(k)) is not None}

    lineage_docs: dict[int, dict[str, Any]] = {}
    for r in sorted(lineages):
        try:
            lineage_docs[r] = json.loads(_gunzip(store.get(Layout.pub(Layout.rel_lineage(r)))))
        except Exception:  # noqa: BLE001
            continue

    # retain recent and weekly snapshots
    keep_snap = {r for r in snapshots if r >= m}
    weeks: dict[tuple[int, int], int] = {}
    for r in snapshots:
        doc = lineage_docs.get(r)
        when = _parse_z(doc["generated_at"]) if doc else None
        if when is None or (now - when).days // 7 > WEEKLY_WEEKS:      # whole weeks
            continue
        week = tuple(when.isocalendar()[:2])
        if week not in weeks or r > weeks[week]:
            weeks[week] = r
    keep_snap |= set(weeks.values())
    keep_rev_meta = {r for r in set(keys_rev(keys, Layout.parse_manifest_key)) | lineages if r >= m} | keep_snap

    keep: set[str] = {Layout.CURRENT, Layout.SEED_CURRENT, Layout.HISTORY_CURRENT, Layout.CHECKPOINT}
    deletable: set[str] = set()
    unknown: list[str] = []
    retained_lineage_ok = all(r in lineage_docs for r in keep_rev_meta if r in lineages)
    retained_icons = {(i["kind"], i["sha256"]) for r in keep_rev_meta for i in lineage_docs.get(r, {}).get("icons", [])}

    candidate_created: dict[int, datetime | None] = {}
    for k in keys:
        if k.startswith(Layout.HISTORY_ARCHIVED):
            keep.add(k)
            continue
        if k in keep:
            continue
        if (r := Layout.parse_manifest_key(k)) is not None or (r := Layout.parse_lineage_key(k)) is not None:
            (keep if r in keep_rev_meta else deletable).add(k)
        elif (r := Layout.parse_snapshot_key(k)) is not None:
            (keep if r in keep_snap else deletable).add(k)
        elif (pair := Layout.parse_update_key(k)) is not None or (pair := Layout.parse_delta_pack_key(k)) is not None:
            (keep if pair[1] >= m else deletable).add(k)
        elif (r := Layout.parse_base_pack_key(k)) is not None:
            (keep if r >= m else deletable).add(k)
        elif (icon := Layout.parse_icon_key(k)) is not None:
            if not retained_lineage_ok or icon in retained_icons:
                keep.add(k)
            else:
                deletable.add(k)
        elif (r := Layout.parse_seed_key(k)) is not None or (r := Layout.parse_history_key(k)) is not None:
            (keep if r in keep_snap else deletable).add(k)
        elif (bid := Layout.parse_backup_key(k)) is not None:
            stamp = _parse_z(f"{bid[0:4]}-{bid[4:6]}-{bid[6:8]}T{bid[9:11]}:{bid[11:13]}:{bid[13:15]}Z") if _BACKUP_ID.match(bid) else None
            if stamp is None:
                unknown.append(k)
            else:
                (deletable if now - stamp > timedelta(days=BACKUP_DAYS) else keep).add(k)
        elif (cand := Layout.parse_candidate_key(k)) is not None:
            r = cand[0]
            if r not in candidate_created:
                try:
                    created = _parse_z(json.loads(store.get(Layout.candidate(r) + "bundle.json")).get("created_at"))
                except Exception:  # noqa: BLE001
                    created = None
                candidate_created[r] = created
            created = candidate_created[r]
            if created is None:
                unknown.append(k)
            elif r > n and now - created < timedelta(days=CANDIDATE_DAYS):
                keep.add(k)
            else:
                deletable.add(k)
        else:
            unknown.append(k)

    # never delete current objects
    try:
        manifest = json.loads(store.get(Layout.pub(current["manifest"])))
        needed = {Layout.pub(p) for p in _bundle_manifest_refs(manifest)} | {Layout.pub(current["manifest"])}
        needed |= {Layout.pub(Layout.rel_icon(i["kind"], i["sha256"])) for i in lineage_docs.get(n, {}).get("icons", [])}
    except Exception as exc:
        raise PublishError(f"cannot read the current manifest, refusing to delete anything: {exc}", step=8) from exc
    clash = needed & deletable
    if clash:
        raise PublishError(f"retention would delete the current release's objects: {sorted(clash)[:3]}", step=8)

    doomed = tuple(sorted(deletable))
    if not dry_run:
        for k in doomed:
            store.delete(k)
    return RetentionReport(tuple(sorted(keep)), doomed, tuple(sorted(unknown)), dry_run)


def keys_rev(keys: Sequence[str], parser: Callable[[str], int | None]) -> list[int]:
    return [r for k in keys if (r := parser(k)) is not None]


__all__ = [
    "Approval", "BuildState", "CandidateMissing", "CandidateStale", "CheckReport", "DirtyBuild", "PostPublishCheckFailed",
    "PublishError", "PublishResult", "RetentionReport", "RevisionConflict", "approve", "current_bytes",
    "fetch_candidate", "heartbeat", "hold", "make_current", "post_publish_check", "publish", "read_current",
    "record_approval", "record_release", "retain", "urllib_fetch",
]
