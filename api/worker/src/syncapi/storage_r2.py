"""r2-binding store for the cloudflare worker"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator

from syncapi.routing import etag_matches
from syncapi.storage import StoredObject, guard

__all__ = ["R2PubStore", "cancel_body", "is_missing"]


def _jsnull() -> object | None:
    try:
        from pyodide.ffi import jsnull  # type: ignore[import-not-found]
    except Exception:
        return None
    return jsnull


def is_missing(obj: object) -> bool:
    """r2 get answers js null for missing key"""
    if obj is None:
        return True
    null = _jsnull()
    if null is not None and obj is null:
        return True
    return getattr(obj, "httpEtag", None) is None


async def cancel_body(obj: object) -> None:
    """cancel unread r2 body stream"""
    try:
        cancel = obj.body.cancel()  # type: ignore[attr-defined]
        if inspect.isawaitable(cancel):
            await cancel
    except Exception:
        pass


class _R2Object:
    def __init__(self, obj: object, etag: str, size: int, *, not_modified: bool = False) -> None:
        self._obj = obj
        self.etag = etag
        self.size = size
        self.not_modified = not_modified
        self._finished = not_modified

    async def read(self) -> bytes:
        if self.not_modified:
            raise ValueError("no body available (not modified)")
        buffer = await self._obj.arrayBuffer()  # type: ignore[attr-defined]
        self._finished = True
        return bytes(buffer.to_bytes())

    async def chunks(self) -> AsyncIterator[bytes]:
        if self.not_modified:
            return
        reader = self._obj.body.getReader()  # type: ignore[attr-defined]
        while True:
            step = await reader.read()
            if step.done:
                self._finished = True
                break
            yield bytes(step.value.to_bytes())

    async def close(self) -> None:
        if not self._finished:
            self._finished = True
            await cancel_body(self._obj)


class R2PubStore:
    def __init__(self, binding: object) -> None:
        self._binding = binding

    async def get(self, key: str, *, if_none_match: str | None = None) -> StoredObject | None:
        key = guard(key)
        obj = await self._binding.get(key)  # type: ignore[attr-defined]  # plain get: no options object to convert
        if is_missing(obj):
            return None
        etag, size = str(obj.httpEtag), int(obj.size)  # type: ignore[attr-defined]
        if etag_matches(if_none_match, etag):
            await cancel_body(obj)
            return _R2Object(obj, etag, size, not_modified=True)
        return _R2Object(obj, etag, size)
