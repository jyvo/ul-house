"""HTTP path
- one requests.Session, one request in flight
- every request (first attempt, retry or redirect hop) takes a throttle slot (requests strictly start at least 'interval' apart)
- stop event checked before every request
- every wait (throttle, backoff) is a stop.wait() so stops land within one in-flight response and raise Stopped
- one response is bounded by `deadline` (+ at most one read_timeout for the chunk in progress):
  the read timeout alone only bounds each socket read, so a slow drip could otherwise run forever
- a body cut off mid-transfer, or one past its deadline, is a transport failure: retried, then FetchError
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests
import urllib3
from requests.structures import CaseInsensitiveDict

from ul_house.config import BASE_URL, USER_AGENT

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
CONDITIONAL_HEADERS = frozenset({"if-none-match", "if-modified-since"})
DEFAULT_PORTS = {"http": 80, "https": 443}
CHUNK_SIZE = 64 * 1024


class Stopped(Exception):
    """the stop event was set; nothing further was sent"""


class FetchError(Exception):
    """the transport failed on every attempt (connection refused, timeout, reset ...)"""


class DeadlineExceeded(Exception):
    """one response took longer than the client's deadline to arrive in full"""


TRANSPORT_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
    urllib3.exceptions.HTTPError,
    DeadlineExceeded,
)


@dataclass(frozen=True)
class Response:
    path: str                        # relative to base url
    status: int
    body: bytes | None               # None for 304 | bodiless
    etag: str | None
    last_modified: str | None
    content_type: str | None
    final_url: str                   # after redirects
    redirect_refused: str | None = None
    headers: Mapping[str, str] = field(default_factory=CaseInsensitiveDict, compare=False, repr=False)

    @property
    def ok(self) -> bool:
        return self.status in (200, 304)


class Throttle:
    """requests start at least 'intervals' apart from when each one was released
        - next slot set from release time, not scheduled slot
            - wait that wakes 3 ms late would let the following request go 3 ms early
        - idle time not banked
    """

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic):
        if interval < 0:
            raise ValueError("interval must be non-negative")
        self.interval = interval
        self._clock = clock
        self._next_at = 0.0

    def widen(self, interval: float) -> None:
        """throttle only ever slows down (robots Crawl-delay)"""
        self.interval = max(self.interval, interval)

    def wait(self, stop: threading.Event) -> None:
        slot = max(self._clock(), self._next_at)
        # re-check clock after every wait (Event.wait can return early)
        while (delay := slot - self._clock()) > 0:
            if stop.wait(delay):
                raise Stopped
        self._next_at = self._clock() + self.interval


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """either delta-seconds or an HTTP-date"""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


class Client:
    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        user_agent: str = USER_AGENT,
        interval: float = 0.15,
        max_retries: int = 4,
        timeout: tuple[float, float] = (5.0, 15.0),
        deadline: float = 30.0,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        retry_after_cap: float = 300.0,
        max_redirects: int = 5,
        stop: threading.Event | None = None,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        retry_statuses: frozenset[int] = RETRY_STATUSES,
    ):
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if deadline <= 0:
            raise ValueError("deadline must be positive")
        self.base_url = base_url.rstrip("/") + "/"
        self._origin = self._origin_of(self.base_url)
        self.timeout = timeout
        self.deadline = deadline
        self._clock = clock
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.retry_after_cap = retry_after_cap
        self.max_redirects = max_redirects
        self.retry_statuses = retry_statuses
        self.stop = stop or threading.Event()
        self.throttle = Throttle(interval, clock)
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def _origin_of(url: str) -> tuple[str, str, int | None]:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        return scheme, (parts.hostname or "").lower(), parts.port or DEFAULT_PORTS.get(scheme)

    def same_origin(self, url: str) -> bool:
        """redirects are pinned (same scheme, host and port)"""
        return self._origin_of(url) == self._origin

    def url_for(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def get(self, path: str, headers: Mapping[str, str] | None = None) -> Response:
        """single GET: conditional headers, pinned redirects, retries; raises Stopped / FetchError"""
        target = self.url_for(path)
        headers = dict(headers or {})
        for _ in range(self.max_redirects + 1):
            reply, body = self._send(target, headers)
            if reply.status_code in REDIRECT_STATUSES and reply.headers.get("Location"):
                nxt = urljoin(target, reply.headers["Location"])
                if not self.same_origin(nxt):
                    return self._response(path, target, reply, body, redirect_refused=nxt)
                target = nxt
                headers = {k: v for k, v in headers.items() if k.lower() not in CONDITIONAL_HEADERS}
                continue
            return self._response(path, target, reply, body)
        raise FetchError(f"more than {self.max_redirects} redirects from {path}")

    def _read_body(self, reply: requests.Response, started: float) -> bytes:
        chunks = []
        while chunk := reply.raw.read1(CHUNK_SIZE, decode_content=True):
            chunks.append(chunk)
            if self._clock() - started > self.deadline:
                raise DeadlineExceeded(f"{reply.url}: body still arriving after {self.deadline:g}s")
        return b"".join(chunks)

    def _send(self, url: str, headers: Mapping[str, str]) -> tuple[requests.Response, bytes]:
        attempt = 0
        while True:
            if self.stop.is_set():
                raise Stopped
            self.throttle.wait(self.stop)
            error: Exception | None = None
            reply: requests.Response | None = None
            body = b""
            try:
                started = self._clock()
                reply = self.session.get(url, headers=dict(headers), timeout=self.timeout,
                                         allow_redirects=False, stream=True)
                body = self._read_body(reply, started)
            except TRANSPORT_ERRORS as exc:
                error = exc
                if reply is not None:
                    reply.close()
                    reply = None
            else:
                if reply.status_code not in self.retry_statuses:
                    return reply, body

            attempt += 1
            if attempt > self.max_retries:
                if reply is not None:
                    return reply, body
                raise FetchError(f"{url}: {type(error).__name__}: {error}") from error

            delay = min(self.backoff_cap, self.backoff_base * 2 ** (attempt - 1))
            hinted = retry_after_seconds(reply.headers.get("Retry-After")) if reply is not None else None
            if hinted is not None:
                delay = min(self.retry_after_cap, max(delay, hinted))
            if self.stop.wait(delay):
                raise Stopped

    @staticmethod
    def _response(path: str, url: str, reply: requests.Response, body: bytes,
                  redirect_refused: str | None = None) -> Response:
        return Response(
            path=path,
            status=reply.status_code,
            body=body if reply.status_code != 304 else None,
            etag=reply.headers.get("ETag"),
            last_modified=reply.headers.get("Last-Modified"),
            content_type=reply.headers.get("Content-Type"),
            final_url=url,
            redirect_refused=redirect_refused,
            headers=CaseInsensitiveDict(reply.headers),
        )
