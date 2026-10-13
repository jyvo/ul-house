"""sync driver"""
from __future__ import annotations

import enum
import posixpath
import queue
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ul_house import database
from ul_house.http.client import RETRY_STATUSES, Client, FetchError, Response, Stopped
from ul_house.sync import apply, snapshot, verify
from ul_house.sync.errors import ApplyError, DatabaseBusy, PackageError, ProtocolError, Refused, RevisionMismatch, SnapshotError, SyncError, VerificationError
from ul_house.sync.icons import IconCache
from ul_house.sync.keys import PUBLIC_KEYS
from ul_house.sync.protocol import ICON_PACK_NAME_RE, ApiError, Manifest, RequiresSnapshot, UpdatePackage, format_utc, loads, utc_now

BASE_PACK_THRESHOLD = 25
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class Outcome(enum.Enum):
    UP_TO_DATE = "up_to_date"
    UPDATED = "updated"
    SNAPSHOT = "snapshot"
    REFUSED = "refused"
    OFFLINE = "offline"
    OVER_CAP = "over_cap"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class SyncConfig:
    api_base_url: str
    db_path: Path
    icon_dir: Path
    app_version: str
    user_agent: str
    public_keys: tuple[str, ...] = PUBLIC_KEYS
    poll_interval: float = 6 * 3600         # 14.4
    launch_jitter: float = 30.0             # seconds before the launch poll
    request_deadline: float = 120.0         # whole-response bound (the snapshot download)
    max_retries: int = 2
    tmp_dir: Path | None = None             # snapshot downloads (default <db dir>/tmp)

    def __post_init__(self) -> None:
        parts = urlsplit(self.api_base_url or "")
        if not parts.hostname:
            raise ValueError("api_base_url must be an absolute URL")
        loopback = (parts.hostname or "").lower() in LOOPBACK_HOSTS
        if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
            raise ValueError("api_base_url must be https (http is accepted only on loopback)")
        if self.poll_interval <= 0 or self.request_deadline <= 0 or self.launch_jitter < 0:
            raise ValueError("poll_interval and request_deadline must be positive, launch_jitter not negative")

    @property
    def snapshot_tmp_dir(self) -> Path:
        return Path(self.tmp_dir) if self.tmp_dir is not None else Path(self.db_path).parent / "tmp"


@dataclass(frozen=True)
class SyncResult:
    outcome: Outcome
    local_revision: int | None
    remote_revision: int | None = None
    stale: bool = False
    reason: str | None = None
    icons_installed: int = 0
    icons_missing: int = 0


def make_client(config: SyncConfig, stop: threading.Event) -> Client:
    return Client(config.api_base_url, user_agent=config.user_agent, max_retries=config.max_retries,
                  deadline=config.request_deadline, retry_after_cap=30.0,
                  retry_statuses=RETRY_STATUSES - {429}, stop=stop)


# one cycle
class _Finish(Exception):
    def __init__(self, result: SyncResult) -> None:
        self.result = result


class _Restart(Exception):
    """current moved during the cycle: start again from /v1/version (once per sync_once)"""


@dataclass
class _Ctx:
    conn: sqlite3.Connection
    client: Client
    cache: IconCache
    keys: Sequence[Ed25519PublicKey]
    app_version: str
    tmp_dir: Path
    now: Callable[[], datetime]
    emit: Callable[[object], None] | None = None

    def announce(self, event: object) -> None:
        if self.emit is not None:
            self.emit(event)


def sync_once(conn: sqlite3.Connection, client: Client, cache: IconCache, *,
              keys: Sequence[Ed25519PublicKey], app_version: str, tmp_dir: Path,
              now: Callable[[], datetime] = utc_now,
              emit: Callable[[object], None] | None = None) -> SyncResult:
    ctx = _Ctx(conn, client, cache, keys, app_version, Path(tmp_dir), now, emit)
    ctx.announce(SyncStarted())
    restarted = False
    while True:
        try:
            return _cycle(ctx)
        except _Finish as done:
            return done.result
        except _Restart:
            if restarted:
                return _result(ctx, Outcome.FAILED, reason="the release changed twice during one cycle")
            restarted = True
        except Stopped:
            return _result(ctx, Outcome.STOPPED)


def _result(ctx: _Ctx, outcome: Outcome, *, remote: int | None = None, stale: bool = False,
            reason: str | None = None, installed: int = 0, missing: int = 0) -> SyncResult:
    try:
        local = database.applied_revision(ctx.conn)
    except sqlite3.Error:
        local = None
    return SyncResult(outcome, local, remote, stale, reason, installed, missing)


def _get(ctx: _Ctx, path: str, headers: dict[str, str] | None = None, *,
         ok: tuple[int, ...] = (200,)) -> Response:
    try:
        response = ctx.client.get(path, headers)
    except FetchError as exc:
        raise _Finish(_result(ctx, Outcome.OFFLINE, reason=str(exc))) from exc
    if response.redirect_refused:
        raise _Finish(_result(ctx, Outcome.FAILED, reason=f"{path}: redirect to another origin refused"))
    if response.status in ok:
        return response
    if response.status == 429:
        platform = "X-API-Commit" not in response.headers
        raise _Finish(_result(ctx, Outcome.OVER_CAP, reason="platform" if platform else "api"))
    if response.status >= 500:
        raise _Finish(_result(ctx, Outcome.OFFLINE, reason=f"{path}: HTTP {response.status}"))
    raise _Finish(_result(ctx, Outcome.FAILED, reason=f"{path}: HTTP {response.status}"))


def _media_type(response: Response) -> str:
    return (response.content_type or "").split(";")[0].strip().lower()


def _cycle(ctx: _Ctx) -> SyncResult:
    conn = ctx.conn
    local = database.applied_revision(conn)
    etag = database.get_state(conn, "current_etag") if local is not None else None

    # pointer
    response = _get(ctx, "v1/version", {"If-None-Match": etag} if etag else None, ok=(200, 304))
    stamp = format_utc(ctx.now())
    if response.status == 304:
        stored = database.get_state(conn, "expires_at")
        database.set_state(conn, {"last_checked_at": stamp})
        stale = bool(stored) and verify.is_stale(stored, ctx.now())
        return _result(ctx, Outcome.UP_TO_DATE, remote=local, stale=stale)

    # signature, versions, revision
    try:
        current = verify.verify_current(response.body or b"", ctx.keys)
        verify.check_supported(current)
        mode = verify.check_revision(current, local)
    except Refused as exc:
        return _result(ctx, Outcome.REFUSED, reason=exc.reason)
    except (VerificationError, ProtocolError) as exc:
        return _result(ctx, Outcome.FAILED, reason=str(exc))
    stale = verify.is_stale(current.expires_at, ctx.now())

    if mode == "same":
        database.set_state(conn, {"current_etag": response.etag, "expires_at": current.expires_at,
                                  "last_checked_at": stamp})
        return _result(ctx, Outcome.UP_TO_DATE, remote=current.revision, stale=stale)

    # manifest
    manifest = _manifest(ctx, current)

    # package or snapshot
    package: UpdatePackage | None = None
    kind = Outcome.SNAPSHOT
    use_snapshot = (local is None or local < current.minimum_revision
                    or manifest.update_from(local) is None)
    if not use_snapshot:
        outcome = _try_package(ctx, current, manifest, local)
        if outcome is None:
            use_snapshot = True
        else:
            package, kind = outcome, Outcome.UPDATED
    if use_snapshot:
        _snapshot(ctx, current, manifest)
        kind = Outcome.SNAPSHOT

    database.set_state(conn, {
        "current_etag": response.etag, "expires_at": current.expires_at, "last_checked_at": stamp,
        "catalog_version": manifest.build.catalog_version,
        "source_last_updated": manifest.freshness.source_last_updated,
        "dataset_generated_at": manifest.freshness.dataset_generated_at,
        "published_at": current.published_at})

    # icon never fails sync
    installed = _icons(ctx, manifest, package, local)
    try:
        missing = len(ctx.cache.missing(conn))
    except (OSError, sqlite3.Error):
        missing = 0
    return _result(ctx, kind, remote=current.revision, stale=stale, installed=installed, missing=missing)


def _manifest(ctx: _Ctx, current) -> Manifest:
    response = _get(ctx, f"v1/manifests/{current.revision}")
    try:
        body = response.body or b""
        if len(body) > verify.MAX_JSON_BYTES:
            raise VerificationError("manifest is too large")
        verify.require_sha256(body, current.manifest_sha256, "manifest")
        manifest = Manifest.from_json(body)
        verify.check_supported(manifest)
        verify.check_invariants(current, manifest)
    except Refused as exc:
        raise _Finish(_result(ctx, Outcome.REFUSED, reason=exc.reason)) from exc
    except (VerificationError, ProtocolError) as exc:
        raise _Finish(_result(ctx, Outcome.FAILED, reason=str(exc))) from exc
    return manifest


def _try_package(ctx: _Ctx, current, manifest: Manifest, local: int) -> UpdatePackage | None:
    ref = manifest.update_from(local)
    assert ref is not None
    response = _get(ctx, f"v1/updates/{local}", ok=(200, 404))
    if response.status == 404:
        error = _api_error(response)
        raise _Finish(_result(ctx, Outcome.FAILED, remote=current.revision,
                              reason="no_update" if error == "no_update" else "HTTP 404"))
    media = _media_type(response)
    if media == "application/json":
        try:
            answer = RequiresSnapshot.from_json(response.body or b"")
        except ProtocolError as exc:
            raise _Finish(_result(ctx, Outcome.FAILED, reason=f"requires_snapshot: {exc}")) from exc
        if answer.snapshot_revision != current.revision:
            raise _Restart
        return None
    if media != "application/gzip":
        raise _Finish(_result(ctx, Outcome.FAILED, reason=f"updates/{local}: unexpected content type {media!r}"))
    to_header = response.headers.get("X-UL-To-Revision")
    if to_header is not None and to_header != str(current.revision):
        raise _Restart
    body = response.body or b""
    try:
        verify.require_sha256(body, ref.sha256, f"update {local}")
    except VerificationError as exc:
        if to_header is None:
            raise _Restart from exc
        raise _Finish(_result(ctx, Outcome.FAILED, reason="package does not match the signed manifest")) from exc
    try:
        package = UpdatePackage.from_json(verify.gunzip_bounded(body, verify.MAX_PACKAGE_BYTES))
        if package.to_revision != current.revision:
            raise PackageError(f"package leads to {package.to_revision}, not {current.revision}")
        apply.apply_package(ctx.conn, package, package_sha256=ref.sha256, app_version=ctx.app_version,
                            now=ctx.now, on_begin=lambda: ctx.announce(Applying("update")))
    except Refused as exc:
        raise _Finish(_result(ctx, Outcome.REFUSED, reason=exc.reason)) from exc
    except RevisionMismatch as exc:
        raise _Finish(_result(ctx, Outcome.FAILED, reason=str(exc))) from exc
    except DatabaseBusy as exc:
        raise _Finish(_result(ctx, Outcome.FAILED, reason="busy")) from exc
    except (ApplyError, PackageError, ProtocolError, VerificationError):
        return None
    return package


def _api_error(response: Response) -> str | None:
    try:
        return ApiError.from_json(loads(response.body or b"")).error
    except (ProtocolError, ValueError):
        return None


def _snapshot(ctx: _Ctx, current, manifest: Manifest) -> None:
    ref = manifest.artifacts.snapshot
    response = _get(ctx, f"v1/snapshots/{current.revision}")
    try:
        snapshot.install_snapshot_blob(
            ctx.conn, response.body or b"", sha256=ref.sha256, revision=current.revision,
            app_version=ctx.app_version, tmp_dir=ctx.tmp_dir, entity_counts=manifest.entity_counts, now=ctx.now,
            on_begin=lambda: ctx.announce(Applying("snapshot")))
    except Refused as exc:
        raise _Finish(_result(ctx, Outcome.REFUSED, reason=exc.reason)) from exc
    except (SnapshotError, VerificationError, RevisionMismatch, ProtocolError, OSError) as exc:
        raise _Finish(_result(ctx, Outcome.FAILED, reason=str(exc))) from exc


def _icons(ctx: _Ctx, manifest: Manifest, package: UpdatePackage | None, local: int | None) -> int:
    installed = 0
    try:
        icon_kinds = ctx.cache.icon_kinds(ctx.conn)
        if package is not None and package.icons.pack is not None and local is not None:
            delta = manifest.delta_pack(local)
            if (delta is not None and package.icons.pack == delta.path
                    and package.icons.pack_sha256 == delta.sha256):
                installed += _install_pack(ctx, delta.path, delta.sha256, icon_kinds, set(package.icons.added))
        elif package is None:
            if len(ctx.cache.missing(ctx.conn)) > BASE_PACK_THRESHOLD:
                base = manifest.artifacts.icon_packs.base
                installed += _install_pack(ctx, base.path, base.sha256, icon_kinds, None)
        ctx.cache.cleanup(ctx.conn, withdrawn=manifest.icons.withdrawn)
    except Stopped:
        raise
    except (SyncError, FetchError, OSError, sqlite3.Error, _Finish):
        pass
    return installed


def _install_pack(ctx: _Ctx, path: str, sha256: str, icon_kinds, wanted) -> int:
    name = posixpath.basename(path)
    if not ICON_PACK_NAME_RE.fullmatch(name):
        return 0
    response = ctx.client.get(f"v1/icons/packs/{name}")
    if response.status != 200 or response.body is None:
        return 0
    result = ctx.cache.install_pack(response.body, expected_sha256=sha256, icon_kinds=icon_kinds,
                                    wanted=wanted, name=name)
    return len(result.installed)


class Schedule:
    def __init__(self, interval: float = 6 * 3600, rng: random.Random | None = None) -> None:
        self.interval = interval
        self.rng = rng or random.Random()
        self.failures = 0

    def first_delay(self, launch_jitter: float) -> float:
        return self.rng.uniform(0.0, launch_jitter)

    def next_delay(self, result: SyncResult, now: datetime) -> float:
        if result.outcome in (Outcome.OFFLINE, Outcome.FAILED):
            self.failures += 1
            base = min(self.interval, 300.0 * 2 ** (self.failures - 1))
            return base * self.rng.uniform(0.5, 1.5)
        self.failures = 0
        if result.outcome is Outcome.OVER_CAP:
            now = now.astimezone(timezone.utc)
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return (midnight - now).total_seconds() + self.rng.uniform(0.0, 3600.0)
        return self.interval * self.rng.uniform(0.9, 1.1)


# worker thread
@dataclass(frozen=True)
class SyncStarted:
    """a cycle begins contacting the API"""


@dataclass(frozen=True)
class Applying:
    kind: Literal["update", "snapshot"]


@dataclass(frozen=True)
class SyncFinished:
    result: SyncResult


@dataclass(frozen=True)
class IconReady:
    kind: str
    sha256: str
    path: Path


@dataclass(frozen=True)
class IconFailed:
    kind: str
    sha256: str


class SyncWorker:
    def __init__(self, config: SyncConfig, *, events: queue.Queue | None = None,
                 schedule: Schedule | None = None, now: Callable[[], datetime] = utc_now,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.events: queue.Queue = events if events is not None else queue.Queue()
        self.schedule = schedule or Schedule(config.poll_interval)
        self._now = now
        self._clock = clock
        self._stop = threading.Event()
        self._jobs: queue.Queue = queue.Queue()
        self._pending_icons: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("the worker is already started")
        self._thread = threading.Thread(target=self.run, name="ul-house-sync", daemon=True)
        self._thread.start()

    def request_sync(self) -> None:
        self._jobs.put(("sync",))

    def request_icon(self, kind: str, sha256: str) -> None:
        IconCache(self.config.icon_dir).path_for(kind, sha256)
        with self._lock:
            if (kind, sha256) in self._pending_icons:
                return
            self._pending_icons.add((kind, sha256))
        self._jobs.put(("icon", kind, sha256))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._jobs.put(("stop",))
        if self._thread is not None:
            self._thread.join(timeout)

    def run(self) -> None:
        conn = client = None
        try:
            try:
                conn = database.open_database(self.config.db_path)
                client = make_client(self.config, self._stop)
                cache = IconCache(self.config.icon_dir)
                keys = verify.load_public_keys(self.config.public_keys)
            except Exception as exc:
                self.events.put(SyncFinished(SyncResult(Outcome.FAILED, None, reason=f"{type(exc).__name__}: {exc}")))
                return
            self._loop(conn, client, cache, keys)
        finally:
            if client is not None:
                client.close()
            if conn is not None:
                conn.close()

    def _loop(self, conn, client, cache, keys) -> None:
        next_at = self._clock() + self.schedule.first_delay(self.config.launch_jitter)
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=max(0.0, next_at - self._clock()))
            except queue.Empty:
                job = ("sync",)
            if self._stop.is_set() or job[0] == "stop":
                break
            if job[0] == "sync":
                result = self._sync(conn, client, cache, keys)
                if result.outcome is Outcome.STOPPED:
                    break
                next_at = self._clock() + self.schedule.next_delay(result, self._now())
            elif job[0] == "icon":
                _, kind, sha = job
                try:
                    path = cache.fetch(client, kind, sha)
                except Stopped:
                    break
                except Exception:
                    path = None
                finally:
                    with self._lock:
                        self._pending_icons.discard((kind, sha))
                self.events.put(IconReady(kind, sha, path) if path is not None else IconFailed(kind, sha))

    def _sync(self, conn, client, cache, keys) -> SyncResult:
        try:
            result = sync_once(conn, client, cache, keys=keys, app_version=self.config.app_version,
                               tmp_dir=self.config.snapshot_tmp_dir, now=self._now, emit=self.events.put)
        except Exception as exc:
            result = SyncResult(Outcome.FAILED, _safe_revision(conn), reason=f"{type(exc).__name__}: {exc}")
        self.events.put(SyncFinished(result))
        return result


def _safe_revision(conn) -> int | None:
    try:
        return database.applied_revision(conn)
    except sqlite3.Error:
        return None
