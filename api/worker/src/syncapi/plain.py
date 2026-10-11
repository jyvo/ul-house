"""handler, same contract without FastAPI or an ASGI bridge"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from syncapi import routing
from syncapi.routing import BelowMinimum, ErrorPlan, NoUpdate, ObjectPlan, Package, PointerError, UpdatesPlan
from syncapi.storage_r2 import cancel_body, is_missing

BUCKET_BINDING = "BUCKET"
COMMIT_VAR = "API_COMMIT"
COMMIT_RE = re.compile(r"[0-9a-f]{40}|dev")

__all__ = ["BUCKET_BINDING", "COMMIT_RE", "COMMIT_VAR", "PlainResponse", "handle"]


@dataclass(frozen=True, slots=True)
class PlainResponse:
    status: int                             # 200 | 304 | 404 | 405 | 500 | 503
    headers: tuple[tuple[str, str], ...]    # includes X-API-Commit and nosniff
    body: bytes | None = None
    stream: object | None = None            # raw R2 obj


def _header(headers: Any, name: str) -> str | None:
    """case-insensitive header lookup over a JS Headers proxy"""
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if value is not None:
        return str(value)
    try:
        for key, val in headers.items():
            if str(key).lower() == name:
                return str(val)
    except Exception:
        pass
    return None


def _commit(env: Any) -> str:
    value = getattr(env, COMMIT_VAR, None)
    if isinstance(value, str) and COMMIT_RE.fullmatch(value):
        return value
    return "dev"


def _finish(commit: str, status: int, headers: list[tuple[str, str]], **kw: Any) -> PlainResponse:
    headers = [*headers, ("X-API-Commit", commit), ("X-Content-Type-Options", "nosniff")]
    body = kw.get("body")
    if body is not None and status != 304:
        headers.append(("Content-Length", str(len(body))))
    return PlainResponse(status, tuple(headers), **kw)


def _error(commit: str, status: int, code: str, message: str) -> PlainResponse:
    headers = [("Content-Type", routing.JSON), ("Cache-Control", routing.NO_STORE)]
    if status == 503:
        headers.append(("Retry-After", str(routing.RETRY_AFTER_SECONDS)))
    if status == 405:
        headers.append(("Allow", "GET"))
    return _finish(commit, status, headers, body=routing.error_body(code, message))


def _plan_error(commit: str, plan: ErrorPlan) -> PlainResponse:
    return _error(commit, plan.status, plan.code, plan.message)


async def _fetch(binding: Any, key: str) -> Any | None:
    if not routing.is_allowed_key(key):
        raise routing.NotAllowed(key)
    obj = await binding.get(key)
    return None if is_missing(obj) else obj


async def _object_response(
    commit: str,
    binding: Any,
    plan: ObjectPlan,
    if_none_match: str | None,
    extra: list[tuple[str, str]] | None = None,
) -> PlainResponse | None:
    obj = await _fetch(binding, plan.key)
    if obj is None:
        return None
    etag, size = str(obj.httpEtag), int(obj.size)
    headers = [("ETag", etag), ("Cache-Control", plan.cache_control), *(extra or [])]
    if routing.etag_matches(if_none_match, etag):
        await cancel_body(obj)
        return _finish(commit, 304, headers)
    headers = [("Content-Type", plan.content_type), ("Content-Length", str(size)), *headers]
    return _finish(commit, 200, headers, stream=obj)


def _requires_snapshot(commit: str, pointer: routing.Pointer, from_revision: int, reason: str, if_none_match: str | None) -> PlainResponse:
    body = routing.requires_snapshot_body(pointer, from_revision, reason)
    etag = routing.strong_etag(body)
    headers = [("ETag", etag), ("Cache-Control", routing.POINTER), ("X-UL-To-Revision", str(pointer.revision))]
    if routing.etag_matches(if_none_match, etag):
        return _finish(commit, 304, headers)
    return _finish(commit, 200, [("Content-Type", routing.JSON), *headers], body=body)


async def handle(method: str, url: str, headers: Mapping[str, str], env: Any) -> PlainResponse:
    """routing.resolve(method, path) -> ObjectPlan / UpdatesPlan / ErrorPlan"""
    commit = _commit(env)
    try:
        plan = routing.resolve(method, urlsplit(url).path)
        if isinstance(plan, ErrorPlan):
            return _plan_error(commit, plan)
        binding = getattr(env, BUCKET_BINDING)
        inm = _header(headers, "if-none-match")
        if isinstance(plan, ObjectPlan):
            response = await _object_response(commit, binding, plan, inm)
            if response is not None:
                return response
            if plan.key == routing.POINTER_KEY:
                return _error(commit, 503, "unavailable", "no release published")
            return _error(commit, 404, "not_found", "not found")
        assert isinstance(plan, UpdatesPlan)
        current = await _fetch(binding, routing.POINTER_KEY)
        if current is None:
            return _error(commit, 503, "unavailable", "no release published")
        raw = bytes((await current.arrayBuffer()).to_bytes())
        try:
            pointer = routing.parse_pointer(raw)
        except PointerError:
            return _error(commit, 503, "unavailable", "release pointer unreadable")
        decision = routing.decide_updates(pointer, plan.from_revision)
        if isinstance(decision, NoUpdate):
            return _error(commit, 404, "no_update", "no update available")
        if isinstance(decision, BelowMinimum):
            return _requires_snapshot(commit, pointer, plan.from_revision, "below_minimum", inm)
        assert isinstance(decision, Package)
        package_plan = ObjectPlan(decision.key, routing.GZIP, routing.POINTER)
        response = await _object_response(
            commit, binding, package_plan, inm, [("X-UL-To-Revision", str(pointer.revision))]
        )
        if response is None:
            return _requires_snapshot(commit, pointer, plan.from_revision, "package_missing", inm)
        return response
    except Exception:
        return _error(commit, 500, "internal", "internal error")
