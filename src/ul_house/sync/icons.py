from __future__ import annotations

import io
import os
import re
import sqlite3
import tempfile
import time
import zipfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from ul_house.http.client import Client, FetchError
from ul_house.sync.errors import IconError, ProtocolError
from ul_house.sync.protocol import ICON_KINDS, SHA256_RE, IconPackListing
from ul_house.sync.verify import MAX_ICON_BYTES, MAX_JSON_BYTES, MAX_PACK_MEMBERS, require_sha256, sha256_hex


INDEX_NAME = "index.json"
_MEMBER_RE = re.compile(r"^(?P<kind>equipment|item|ability)/(?P<sha>[0-9a-f]{64})\.png$")
_FILE_RE = re.compile(r"^[0-9a-f]{64}\.png$")
_TMP_RE = re.compile(r"^\.[0-9a-f]{64}\.[A-Za-z0-9_]+\.tmp$")


@dataclass(frozen=True)
class PackResult:
    installed: tuple[str, ...]              # sha256 of every icon stored
    discarded: tuple[str, ...]              # sha mismatch, wrong size, or a kind the local icon table disagrees with
    ignored: tuple[str, ...]                # members outside the layout, unlisted, duplicated or not wanted
    missing: tuple[str, ...] = ()           # listed in index.json but absent from the zip (left to lazy fetch)


class IconCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, kind: str, sha256: str) -> Path:
        if kind not in ICON_KINDS:
            raise ValueError(f"unknown icon kind {kind!r}")
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise ValueError("an icon address is 64 lowercase hex characters")
        return self.root / kind / f"{sha256}.png"

    def has(self, kind: str, sha256: str) -> bool:
        return self.path_for(kind, sha256).is_file()

    def load(self, kind: str, sha256: str) -> bytes | None:
        # bytes rehashed
        path = self.path_for(kind, sha256)
        try:
            if path.stat().st_size > MAX_ICON_BYTES:
                data = None
            else:
                data = path.read_bytes()
        except OSError:
            return None
        if data is None or sha256_hex(data) != sha256:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return data

    def store(self, kind: str, sha256: str, data: bytes) -> Path:
        final = self.path_for(kind, sha256)
        if len(data) > MAX_ICON_BYTES:
            raise IconError(f"icon {sha256} exceeds {MAX_ICON_BYTES} bytes")
        if sha256_hex(data) != sha256:
            raise IconError(f"icon bytes do not hash to {sha256}")
        final.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=final.parent, prefix=f".{sha256}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            try:
                os.replace(tmp, final)
            except PermissionError:
                if self.load(kind, sha256) is None:
                    raise
                os.unlink(tmp)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        return final

    def install_pack(self, blob: bytes, *, expected_sha256: str, icon_kinds: Mapping[str, str],
                     wanted: Collection[str] | None = None, name: str | None = None) -> PackResult:
        require_sha256(blob, expected_sha256, "icon pack")
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile as exc:
            raise IconError(f"icon pack is not a zip: {exc}") from exc
        with zf:
            infos = zf.infolist()
            if len(infos) > MAX_PACK_MEMBERS + 1:
                raise IconError(f"icon pack has {len(infos)} members")
            if not infos or infos[0].filename != INDEX_NAME:
                raise IconError("icon pack: index.json must be the first member")
            if infos[0].file_size > MAX_JSON_BYTES:
                raise IconError("icon pack: index.json is too large")
            try:
                listing = IconPackListing.from_json(zf.read(infos[0]))
            except (ProtocolError, zipfile.BadZipFile, RuntimeError) as exc:
                raise IconError(f"icon pack: invalid index.json: {exc}") from exc
            if name is not None and listing.name != name:
                raise IconError(f"icon pack is named {listing.name!r}, expected {name!r}")
            entries = {(e.kind, e.sha256): e for e in listing.icons}
            wanted_set = None if wanted is None else set(wanted)
            installed: list[str] = []
            discarded: list[str] = []
            ignored: list[str] = []
            seen: set[str] = set()
            for info in infos[1:]:
                member = info.filename
                match = _MEMBER_RE.fullmatch(member)
                if match is None or member in seen:
                    ignored.append(member[:80])
                    continue
                seen.add(member)
                kind, sha = match.group("kind"), match.group("sha")
                entry = entries.get((kind, sha))
                if entry is None or entry.path != member:
                    ignored.append(member)
                    continue
                if wanted_set is not None and sha not in wanted_set:
                    ignored.append(member)
                    continue
                local_kind = icon_kinds.get(sha)
                if local_kind is None:
                    ignored.append(member)
                    continue
                if local_kind != kind or info.file_size != entry.bytes or entry.bytes > MAX_ICON_BYTES:
                    discarded.append(sha)
                    continue
                try:
                    with zf.open(info) as handle:
                        data = handle.read(MAX_ICON_BYTES + 1)
                except (zipfile.BadZipFile, RuntimeError, EOFError, OSError):
                    discarded.append(sha)
                    continue
                if len(data) != entry.bytes or sha256_hex(data) != sha:
                    discarded.append(sha)
                    continue
                self.store(kind, sha, data)
                installed.append(sha)
            missing = [e.sha256 for e in listing.icons if e.path not in seen]
        return PackResult(tuple(installed), tuple(discarded), tuple(ignored), tuple(missing))

    def fetch(self, client: Client, kind: str, sha256: str) -> Path | None:
        path = self.path_for(kind, sha256)
        if self.load(kind, sha256) is not None:
            return path
        try:
            response = client.get(f"v1/icons/{kind}/{sha256}")
        except FetchError:
            return None
        if response.status != 200 or response.body is None:
            return None
        try:
            return self.store(kind, sha256, response.body)
        except IconError:
            return None

    # helpers
    def missing(self, conn: sqlite3.Connection) -> list[tuple[str, str]]:
        """(kind, sha256) of every icon in the local icon table that is not cached"""
        rows = conn.execute("SELECT kind, sha256 FROM main.icon ORDER BY kind, sha256").fetchall()
        return [(k, s) for k, s in rows if k in ICON_KINDS and SHA256_RE.fullmatch(s) and not self.has(k, s)]

    def icon_kinds(self, conn: sqlite3.Connection) -> dict[str, str]:
        """sha256 -> kind from the local icon table"""
        return {s: k for k, s in conn.execute("SELECT kind, sha256 FROM main.icon")}

    def cleanup(self, conn: sqlite3.Connection, *, withdrawn: Collection[str] = (),
                now=time.time, tmp_age: float = 3600.0) -> int:
        """every withdrawn icon (manifest `icons.withdrawn`), and stale `.tmp` files"""
        keep = {(k, s) for k, s in conn.execute("SELECT kind, sha256 FROM main.icon")}
        gone = set(withdrawn)
        deleted = 0
        for kind in ICON_KINDS:
            directory = self.root / kind
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    if _FILE_RE.fullmatch(entry.name):
                        sha = entry.name[:-4]
                        if (kind, sha) in keep and sha not in gone:
                            continue
                    elif _TMP_RE.fullmatch(entry.name):
                        if now() - entry.stat(follow_symlinks=False).st_mtime < tmp_age:
                            continue
                    else:
                        continue
                    os.unlink(entry.path)
                    deleted += 1
                except OSError:
                    continue
        return deleted
