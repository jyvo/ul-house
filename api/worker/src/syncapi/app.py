"""fastapi app"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import StreamingResponse

from syncapi import routing
from syncapi.routing import (
    BelowMinimum,
    ErrorPlan,
    NoUpdate,
    ObjectPlan,
    Package,
    PointerError,
    UpdatesPlan,
    etag_matches,
    strong_etag,
)
from syncapi.storage import PubStore
from syncapi.storage_r2 import R2PubStore

__all__ = ["BUCKET_BINDING", "COMMIT_RE", "COMMIT_VAR", "CommitHeaders", "create_app"]

BUCKET_BINDING = "BUCKET"
COMMIT_VAR = "API_COMMIT"
COMMIT_RE = re.compile(r"[0-9a-f]{40}|dev")

ASGIApp = Callable[..., Awaitable[None]]


def _error_response(status: int, code: str, message: str) -> Response:
    headers = {"Cache-Control": routing.NO_STORE}
    if status == 503:
        headers["Retry-After"] = str(routing.RETRY_AFTER_SECONDS)
    if status == 405:
        headers["Allow"] = "GET"
    return Response(routing.error_body(code, message), status_code=status, media_type=routing.JSON, headers=headers)


def _plan_error(plan: ErrorPlan) -> Response:
    return _error_response(plan.status, plan.code, plan.message)


def _store_for(request: Request, explicit: PubStore | None) -> PubStore:
    if explicit is not None:
        return explicit
    env = request.scope["env"]
    return R2PubStore(getattr(env, BUCKET_BINDING))


async def _serve(request: Request, store: PubStore, plan: ObjectPlan, extra: dict[str, str] | None = None) -> Response | None:
    """stream obj (200), answer 304, or return None if obj not found"""
    obj = await store.get(plan.key, if_none_match=request.headers.get("if-none-match"))
    if obj is None:
        return None
    headers = {"ETag": obj.etag, "Cache-Control": plan.cache_control, **(extra or {})}
    if obj.not_modified:
        await obj.close()
        return Response(status_code=304, headers=headers)
    headers["Content-Length"] = str(obj.size)
    return StreamingResponse(obj.chunks(), media_type=plan.content_type, headers=headers, background=BackgroundTask(obj.close))


def _requires_snapshot(request: Request, pointer: routing.Pointer, from_revision: int, reason: str) -> Response:
    body = routing.requires_snapshot_body(pointer, from_revision, reason)
    etag = strong_etag(body)
    headers = {
        "ETag": etag,
        "Cache-Control": routing.POINTER,
        "X-UL-To-Revision": str(pointer.revision),
    }
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type=routing.JSON, headers=headers)


def _guarded(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
    async def run(request: Request) -> Response:
        try:
            return await handler(request)
        except Exception:
            return _error_response(500, "internal", "internal error")

    run.__name__ = handler.__name__
    return run


class _Gatekeeper:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            if scope["method"] != "GET":
                await _error_response(405, "method_not_allowed", "only GET is supported")(scope, receive, send)
                return
            raw = scope.get("raw_path")
            if raw is None:
                raw = scope["path"].encode("utf-8", "surrogateescape")
            if b"%" in raw:
                await _error_response(404, "not_found", "not found")(scope, receive, send)
                return
        await self.app(scope, receive, send)


class CommitHeaders:
    """ASGI wrapper outside fastapi"""

    def __init__(self, app: ASGIApp, *, api_commit: str | None = None) -> None:
        if api_commit is not None and not COMMIT_RE.fullmatch(api_commit):
            raise ValueError(f"api_commit must be 40 lowercase hex characters or 'dev', got {api_commit!r}")
        self.app = app
        self.api_commit = api_commit

    @property
    def fastapi(self) -> Any:
        return self.app

    def _commit(self, scope: dict[str, Any]) -> str:
        if self.api_commit is not None:
            return self.api_commit
        value = getattr(scope.get("env"), COMMIT_VAR, None)
        if isinstance(value, str) and COMMIT_RE.fullmatch(value):
            return value
        return "dev"

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        commit = self._commit(scope).encode("ascii")

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in (b"x-api-commit", b"x-content-type-options")
                ]
                headers.append((b"x-api-commit", commit))
                headers.append((b"x-content-type-options", b"nosniff"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(store: PubStore | None = None, *, api_commit: str | None = None) -> ASGIApp:
    """store None => per-request R2PubStore(getattr(scope['env'], BUCKET_BINDING)) (the Worker).
    api_commit None => scope['env'].API_COMMIT if valid, else 'dev'. An explicit value that does not
    match COMMIT_RE raises ValueError. Returns CommitHeaders(fastapi_app)."""
    fastapi_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    fastapi_app.add_middleware(_Gatekeeper)

    @fastapi_app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return _error_response(404, "not_found", "not found")
        if exc.status_code == 405:
            return _error_response(405, "method_not_allowed", "only GET is supported")
        return _error_response(500, "internal", "internal error")

    @fastapi_app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> Response:
        return _error_response(500, "internal", "internal error")

    async def respond_object(request: Request, plan: ObjectPlan | ErrorPlan) -> Response:
        if isinstance(plan, ErrorPlan):
            return _plan_error(plan)
        response = await _serve(request, _store_for(request, store), plan)
        return response if response is not None else _error_response(404, "not_found", "not found")

    @fastapi_app.get("/v1/version")
    @_guarded
    async def version(request: Request) -> Response:
        plan = routing.plan_version()
        response = await _serve(request, _store_for(request, store), plan)
        if response is None:
            return _error_response(503, "unavailable", "no release published")
        return response

    @fastapi_app.get("/v1/manifests/{revision}")
    @_guarded
    async def manifest(request: Request) -> Response:
        return await respond_object(request, routing.plan_manifest(request.path_params["revision"]))

    @fastapi_app.get("/v1/updates/{from_revision}")
    @_guarded
    async def updates(request: Request) -> Response:
        plan = routing.plan_updates(request.path_params["from_revision"])
        if isinstance(plan, ErrorPlan):
            return _plan_error(plan)
        assert isinstance(plan, UpdatesPlan)
        pub = _store_for(request, store)
        current = await pub.get(routing.POINTER_KEY)
        if current is None:
            return _error_response(503, "unavailable", "no release published")
        try:
            raw = await current.read()
        finally:
            await current.close()
        try:
            pointer = routing.parse_pointer(raw)
        except PointerError:
            return _error_response(503, "unavailable", "release pointer unreadable")
        decision = routing.decide_updates(pointer, plan.from_revision)
        if isinstance(decision, NoUpdate):
            return _error_response(404, "no_update", "no update available")
        if isinstance(decision, BelowMinimum):
            return _requires_snapshot(request, pointer, plan.from_revision, "below_minimum")
        assert isinstance(decision, Package)
        object_plan = ObjectPlan(decision.key, routing.GZIP, routing.POINTER)
        response = await _serve(request, pub, object_plan, {"X-UL-To-Revision": str(pointer.revision)})
        if response is None:
            return _requires_snapshot(request, pointer, plan.from_revision, "package_missing")
        return response

    @fastapi_app.get("/v1/snapshots/{revision}")
    @_guarded
    async def snapshot(request: Request) -> Response:
        return await respond_object(request, routing.plan_snapshot(request.path_params["revision"]))

    @fastapi_app.get("/v1/icons/packs/{name}")
    @_guarded
    async def pack(request: Request) -> Response:
        return await respond_object(request, routing.plan_pack(request.path_params["name"]))

    @fastapi_app.get("/v1/icons/{kind}/{sha256}")
    @_guarded
    async def icon(request: Request) -> Response:
        params = request.path_params
        return await respond_object(request, routing.plan_icon(params["kind"], params["sha256"]))

    @fastapi_app.get("/v1/lineage/{revision}")
    @_guarded
    async def lineage(request: Request) -> Response:
        return await respond_object(request, routing.plan_lineage(request.path_params["revision"]))

    return CommitHeaders(fastapi_app, api_commit=api_commit)
