"""fastapi, fastapi_r2, plain, static, live: five API targets for testing"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Target:
    name: str
    base_url: str
    store_root: Path | None
    public_keys: tuple[bytes, ...]
    expected_commit: str | None


class UvicornThread:
    def __init__(self, app) -> None:
        import uvicorn

        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        config = uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False, server_header=False, date_header=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=lambda: self.server.run(sockets=[self.sock]), daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def __enter__(self) -> "UvicornThread":
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.01)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.sock.close()


class EnvScope:
    # ASGI wrapper
    def __init__(self, app, env) -> None:
        self.app = app
        self.env = env

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "env": self.env}
        await self.app(scope, receive, send)


class CIHeaders(Mapping):
    def __init__(self, pairs) -> None:
        self._data = {key.lower(): value for key, value in pairs}

    def __getitem__(self, key: str) -> str:
        return self._data[key.lower()]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key, default=None):
        return self._data.get(str(key).lower(), default)


async def drain(stream) -> bytes:
    reader = stream.body.getReader()
    out = bytearray()
    while True:
        step = await reader.read()
        if step.done:
            break
        out += step.value.to_bytes()
    return bytes(out)


class PlainASGI:
    def __init__(self, env) -> None:
        self.env = env

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        from syncapi.plain import handle

        raw_path = scope.get("raw_path") or scope["path"].encode("latin-1")
        url = f"http://{scope['server'][0]}:{scope['server'][1]}{raw_path.decode('latin-1')}"
        headers = CIHeaders((k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"])
        response = await handle(scope["method"], url, headers, self.env)
        body = await drain(response.stream) if response.stream is not None else (response.body or b"")
        raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in response.headers]
        await send({"type": "http.response.start", "status": response.status, "headers": raw_headers})
        await send({"type": "http.response.body", "body": body})


def split_url(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return parts.hostname or "127.0.0.1", parts.port or 80


def run(coro):
    # coroutine runner from sync test code
    return asyncio.run(coro)
