"""http /test
    - server.route("/a.html", Reply(200, b"<html>", {"ETag": '"1"'}))
    - server.route("/b.html", [Reply(503), Reply(200, b"ok")])
        - one reply per request, last repeats
    - server.route("/c.html", lambda request: Reply(304) if request.headers.get("If-None-Match") else Reply(200, b"x"))
    - every request logged with header + arrival time
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


@dataclass
class Seen:
    path: str
    at: float
    headers: dict[str, str]


class LocalServer:
    def __init__(self):
        self.routes: dict[str, object] = {}
        self.log: list[Seen] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.log.append(Seen(self.path, time.monotonic(), dict(self.headers)))
                reply = owner._reply_for(self)
                if reply.delay:
                    time.sleep(reply.delay)
                self.send_response(reply.status)
                for key, value in reply.headers.items():
                    self.send_header(key, value)
                if reply.status not in (204, 304):
                    self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                if reply.status not in (204, 304):
                    self.wfile.write(reply.body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def route(self, path: str, reply: Reply | list[Reply] | Callable) -> None:
        self.routes[path] = list(reply) if isinstance(reply, list) else reply

    def _reply_for(self, request) -> Reply:
        route = self.routes.get(request.path)
        if route is None:
            return Reply(404, b"not found")
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        if callable(route):
            return route(request)
        return route

    def hits(self, path: str) -> list[Seen]:
        return [seen for seen in self.log if seen.path == path]

    def __enter__(self) -> LocalServer:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
