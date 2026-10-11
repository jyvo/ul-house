"""validate contract implementation on static files"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

JSON, GZIP, ZIP, PNG = "application/json", "application/gzip", "application/zip", "image/png"
IMMUTABLE = "public, max-age=31536000, immutable"
POINTER = "public, max-age=300"
NO_STORE = "no-store"
COMMIT = "dev"


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _error(code: str, message: str) -> bytes:
    return _canonical({"error": code, "message": message})


def render(store_root: Path, out_dir: Path) -> dict:
    store_root, out_dir = Path(store_root), Path(out_dir)
    pub = store_root / "pub"
    meta: dict[str, dict] = {}

    def put(request_path: str, data: bytes, *, status: int, content_type: str, cache_control: str, extra: dict | None = None):
        target = out_dir / request_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        meta[request_path] = {"status": status, "content_type": content_type, "cache_control": cache_control, "extra": extra or {}}

    def copy(request_path: str, relative: str, content_type: str, cache_control: str, extra: dict | None = None):
        put(request_path, (pub / relative).read_bytes(), status=200, content_type=content_type, cache_control=cache_control, extra=extra)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    current_bytes = (pub / "current.json").read_bytes()
    current = json.loads(current_bytes)
    n, minimum = current["revision"], current["minimum_revision"]

    copy("/v1/version", "current.json", JSON, POINTER)
    for path in sorted((pub / "manifests").glob("*.json")):
        copy(f"/v1/manifests/{path.stem}", f"manifests/{path.name}", JSON, IMMUTABLE)
    for path in sorted((pub / "snapshots").glob("unison-*.sqlite.gz")):
        revision = re.fullmatch(r"unison-([1-9][0-9]*)\.sqlite\.gz", path.name).group(1)
        copy(f"/v1/snapshots/{revision}", f"snapshots/{path.name}", GZIP, IMMUTABLE)
    for path in sorted((pub / "lineage").glob("*.json.gz")):
        copy(f"/v1/lineage/{path.name[: -len('.json.gz')]}", f"lineage/{path.name}", GZIP, IMMUTABLE)
    for path in sorted((pub / "icons" / "packs").glob("*.zip")):
        copy(f"/v1/icons/packs/{path.name}", f"icons/packs/{path.name}", ZIP, IMMUTABLE)
    for kind_dir in sorted(p for p in (pub / "icons").iterdir() if p.is_dir() and p.name != "packs"):
        for path in sorted(kind_dir.glob("*.png")):
            copy(f"/v1/icons/{kind_dir.name}/{path.stem}", f"icons/{kind_dir.name}/{path.name}", PNG, IMMUTABLE)

    extra = {"X-UL-To-Revision": str(n)}
    for from_revision in range(0, n + 2):
        request_path = f"/v1/updates/{from_revision}"
        if from_revision >= n:
            put(request_path, _error("no_update", "no newer revision"), status=404, content_type=JSON, cache_control=NO_STORE)
            continue
        if from_revision < minimum:
            reason = "below_minimum"
        elif (pub / f"updates/{from_revision}-{n}.json.gz").is_file():
            copy(request_path, f"updates/{from_revision}-{n}.json.gz", GZIP, POINTER, extra)
            continue
        else:
            reason = "package_missing"
        body = _canonical({"requires_snapshot": True, "snapshot_revision": n, "from_revision": from_revision, "reason": reason})
        put(request_path, body, status=200, content_type=JSON, cache_control=POINTER, extra=extra)

    (out_dir / "_meta.json").write_text(json.dumps(meta, sort_keys=True))
    return meta


class StaticServer:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = Path(out_dir)
        self.meta = json.loads((self.out_dir / "_meta.json").read_text())
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, body: bytes, content_type: str | None, headers: dict[str, str]):
                self.send_response(status)
                self.send_header("X-API-Commit", COMMIT)
                self.send_header("X-Content-Type-Options", "nosniff")
                if content_type:
                    self.send_header("Content-Type", content_type)
                for key, value in headers.items():
                    self.send_header(key, value)
                if status != 304:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if status != 304:
                    self.wfile.write(body)

            def do_GET(self):
                entry = owner.meta.get(self.path)
                if entry is None:
                    self._send(404, _error("not_found", "not found"), JSON, {"Cache-Control": NO_STORE})
                    return
                data = (owner.out_dir / self.path.lstrip("/")).read_bytes()
                headers = {"Cache-Control": entry["cache_control"], **entry["extra"]}
                if entry["status"] == 200:
                    etag = '"' + hashlib.sha256(data).hexdigest() + '"'
                    headers["ETag"] = etag
                    wanted = self.headers.get("If-None-Match")
                    if wanted and (wanted.strip() == "*" or etag in [part.strip().removeprefix("W/") for part in wanted.split(",")]):
                        self._send(304, b"", None, headers)
                        return
                self._send(entry["status"], data, entry["content_type"], headers)

            def _not_allowed(self):
                self._send(405, _error("method_not_allowed", "GET only"), JSON, {"Allow": "GET", "Cache-Control": NO_STORE})

            do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _not_allowed

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "StaticServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
