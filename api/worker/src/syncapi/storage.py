"""store interace + local dir implementation (dev + tests)"""
from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO, Protocol

from syncapi.routing import NotAllowed, etag_matches, is_allowed_key

__all__ = ["LocalPubStore", "NotAllowed", "PubStore", "StoredObject", "guard"]


class StoredObject(Protocol):
    etag: str
    size: int
    not_modified: bool

    async def read(self) -> bytes:
        """Whole body (used for current.json only)."""
        ...

    def chunks(self) -> AsyncIterator[bytes]:
        """Streaming body."""
        ...

    async def close(self) -> None:
        """Idempotent, releases the file handle / cancels the stream."""
        ...


class PubStore(Protocol):
    async def get(self, key: str, *, if_none_match: str | None = None) -> StoredObject | None:
        """key in pub/.... Raises NotAllowed outside allow-list."""
        ...


def guard(key: str) -> str:
    """Return key unchanged if on allow-list, else raise NotAllowed."""
    if not is_allowed_key(key):
        raise NotAllowed(f"key not allowed: {key!r}")
    return key


class _LocalObject:
    def __init__(self, handle: BinaryIO | None, etag: str, size: int, *, not_modified: bool, chunk_size: int) -> None:
        self._handle = handle
        self.etag = etag
        self.size = size
        self.not_modified = not_modified
        self._chunk_size = chunk_size

    async def read(self) -> bytes:
        if self._handle is None:
            raise ValueError("no body available (not modified or closed)")
        try:
            return self._handle.read()
        finally:
            await self.close()

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            while self._handle is not None:
                data = self._handle.read(self._chunk_size)
                if not data:
                    break
                yield data
        finally:
            await self.close()

    async def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()


class LocalPubStore:
    def __init__(self, root: Path, *, chunk_size: int = 64 * 1024) -> None:
        self._root = Path(root)
        self._chunk_size = chunk_size
        self._etags: dict[tuple[str, int, int], str] = {}

    async def get(self, key: str, *, if_none_match: str | None = None) -> StoredObject | None:
        guard(key)
        path = self._root / key
        pub = (self._root / "pub").resolve()
        try:
            if not path.is_file():
                return None
            resolved = path.resolve()
            if pub != resolved and pub not in resolved.parents:
                return None
            handle = open(path, "rb")
        except OSError:
            return None
        try:
            st = os.fstat(handle.fileno())
            cache_key = (str(resolved), st.st_size, st.st_mtime_ns)
            etag = self._etags.get(cache_key)
            if etag is None:
                digest = hashlib.sha256()
                while block := handle.read(self._chunk_size):
                    digest.update(block)
                handle.seek(0)
                etag = '"' + digest.hexdigest() + '"'
                self._etags[cache_key] = etag
            if etag_matches(if_none_match, etag):
                handle.close()
                return _LocalObject(None, etag, st.st_size, not_modified=True, chunk_size=self._chunk_size)
            return _LocalObject(handle, etag, st.st_size, not_modified=False, chunk_size=self._chunk_size)
        except BaseException:
            handle.close()
            raise
