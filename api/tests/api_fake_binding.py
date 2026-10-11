"""mimics the workers r2 js surface"""
from __future__ import annotations

import hashlib
from pathlib import Path


class JsNull:
    """stand-in for pyodide.ffi.jsnull: what a JS `null` becomes when it is not converted to None"""

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "null"

    __str__ = __repr__


class Bytes:
    """Uint8Array / ArrayBuffer proxy"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def to_bytes(self) -> bytes:
        return self._data

    @property
    def byteLength(self) -> int:
        return len(self._data)


class Step:
    def __init__(self, done: bool, value: Bytes | None = None) -> None:
        self.done = done
        self.value = value


class Reader:
    def __init__(self, data: bytes, chunk_size: int, owner: "FakeR2Object") -> None:
        self._data = data
        self._chunk = chunk_size
        self._pos = 0
        self._owner = owner

    async def read(self) -> Step:
        if self._pos >= len(self._data):
            return Step(True)
        piece = self._data[self._pos : self._pos + self._chunk]
        self._pos += len(piece)
        self._owner.bytes_streamed += len(piece)
        return Step(False, Bytes(piece))

    async def cancel(self, *_args) -> None:
        self._pos = len(self._data)

    def releaseLock(self) -> None:
        pass


class Body:
    def __init__(self, owner: "FakeR2Object") -> None:
        self._owner = owner

    def getReader(self) -> Reader:
        return Reader(self._owner._data, self._owner._chunk, self._owner)

    async def cancel(self, *_args) -> None:
        pass


class FakeR2Object:
    def __init__(self, key: str, data: bytes, chunk_size: int) -> None:
        self.key = key
        self._data = data
        self._chunk = chunk_size
        self.size = len(data)
        self.httpEtag = '"' + hashlib.md5(data).hexdigest() + '"'
        self.body = Body(self)
        self.bytes_streamed = 0

    async def arrayBuffer(self) -> Bytes:
        return Bytes(self._data)


class FakeR2Binding:
    def __init__(self, root: Path, *, chunk_size: int = 16, missing_sentinel: bool = False) -> None:
        self.root = Path(root)
        self.chunk_size = max(3, chunk_size)
        self.missing = JsNull() if missing_sentinel else None
        self.requested: list[str] = []

    async def get(self, key: str, *args):
        self.requested.append(key)
        path = self.root / key
        if not path.is_file():
            return self.missing
        return FakeR2Object(key, path.read_bytes(), self.chunk_size)


class RaisingBinding:
    """a binding whose get() always fails, for the 500 paths"""

    def __init__(self) -> None:
        self.requested: list[str] = []

    async def get(self, key: str, *args):
        self.requested.append(key)
        raise RuntimeError("simulated R2 outage")
