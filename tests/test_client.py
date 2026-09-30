import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

from local_server import LocalServer, Reply
from ul_house.config import USER_AGENT
from ul_house.http.client import Client, FetchError, Stopped, Throttle, retry_after_seconds


@pytest.fixture
def server():
    with LocalServer() as s:
        yield s


def make_client(server, **kw):
    kw.setdefault("interval", 0.0)
    kw.setdefault("backoff_base", 0.01)
    kw.setdefault("timeout", (1.0, 2.0))
    return Client(server.base_url, **kw)


class DummyClock:
    """clock + stop event > wait() test throttle time creep (wakes early)"""

    def __init__(self, early_by: float = 0.0):
        self.now = 100.0
        self.early_by = early_by
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    def wait(self, delay: float) -> bool:
        self.waits.append(delay)
        early, self.early_by = self.early_by, 0.0
        self.now += delay - min(early, delay / 2)
        return False

    def is_set(self) -> bool:
        return False


class TestThrottle:
    def test_first_slot_immediate_then_spaced(self):
        clock = DummyClock()
        throttle = Throttle(0.15, clock)
        starts = []
        for _ in range(4):
            throttle.wait(clock)
            starts.append(clock.now)
        assert [round(b - a, 9) for a, b in zip(starts, starts[1:])] == [0.15, 0.15, 0.15]

    def test_early_wake_sleeps_again(self):
        clock = DummyClock(early_by=0.004)
        throttle = Throttle(0.15, clock)
        throttle.wait(clock)
        start = clock.now
        throttle.wait(clock)
        assert clock.now - start >= 0.15
        assert len(clock.waits) >= 2, "one early wake must be followed by another sleep"

    def test_idle_time_not_banked(self):
        clock = DummyClock()
        throttle = Throttle(0.15, clock)
        throttle.wait(clock)
        clock.now += 10 
        throttle.wait(clock)
        first = clock.now
        throttle.wait(clock)
        assert clock.now - first == pytest.approx(0.15)

    def test_widens_only(self):
        throttle = Throttle(0.15)
        throttle.widen(1.0)
        throttle.widen(0.01)
        assert throttle.interval == 1.0

    def test_stop_during_wait(self):
        stop = threading.Event()
        throttle = Throttle(5.0)
        throttle.wait(stop)
        threading.Timer(0.05, stop.set).start()
        began = time.monotonic()
        with pytest.raises(Stopped):
            throttle.wait(stop)
        assert time.monotonic() - began < 0.5


class TestGet:
    def test_user_agent_and_body(self, server):
        server.route("/a.html", Reply(200, b"<html>a</html>", {"ETag": '"1"', "Content-Type": "text/html"}))
        with make_client(server) as client:
            reply = client.get("/a.html")
        assert (reply.status, reply.body, reply.etag, reply.content_type) == (200, b"<html>a</html>", '"1"', "text/html")
        assert server.hits("/a.html")[0].headers["User-Agent"] == USER_AGENT

    def test_conditional_get(self, server):
        server.route("/a.html", lambda req: Reply(304, headers={"ETag": '"1"'})
                     if req.headers.get("If-None-Match") == '"1"' else Reply(200, b"x", {"ETag": '"1"'}))
        with make_client(server) as client:
            assert client.get("/a.html").status == 200
            reply = client.get("/a.html", {"If-None-Match": '"1"'})
        assert (reply.status, reply.body, reply.ok) == (304, None, True)

    def test_404_returned_not_retried(self, server):
        with make_client(server) as client:
            assert client.get("/missing.html").status == 404
        assert len(server.hits("/missing.html")) == 1

    def test_requests_are_spaced_by_interval(self, server):
        """measured where the promise is made: when the client sends, not when the server logs"""
        server.route("/a.html", Reply(200, b"x"))
        sent = []

        class Recording(requests.Session):
            def get(self, *args, **kwargs):
                sent.append(time.monotonic())
                return super().get(*args, **kwargs)

        with make_client(server, interval=0.05, session=Recording()) as client:
            for _ in range(6):
                client.get("/a.html")
        gaps = [b - a for a, b in zip(sent, sent[1:])]
        assert len(sent) == 6
        assert min(gaps) >= 0.05


class TestRetries:
    def test_retry_then_success(self, server):
        server.route("/a.html", [Reply(503), Reply(502), Reply(200, b"ok")])
        with make_client(server) as client:
            assert client.get("/a.html").body == b"ok"
        assert len(server.hits("/a.html")) == 3

    def test_exhausted_returns_last_reply(self, server):
        server.route("/a.html", Reply(503))
        with make_client(server, max_retries=2) as client:
            assert client.get("/a.html").status == 503
        assert len(server.hits("/a.html")) == 3

    def test_honoured_retry(self, server):
        server.route("/a.html", [Reply(429, headers={"Retry-After": "1"}), Reply(200, b"ok")])
        with make_client(server) as client:
            client.get("/a.html")
        first, second = server.hits("/a.html")
        assert second.at - first.at >= 0.95

    def test_retries_take_throttle_slots(self, server):
        server.route("/a.html", [Reply(503), Reply(200, b"ok")])
        with make_client(server, interval=0.2, backoff_base=0.0) as client:
            client.get("/a.html")
        first, second = server.hits("/a.html")
        assert second.at - first.at >= 0.2 - 0.002

    def test_transport_failure_raises_after_retries(self):
        with Client("http://127.0.0.1:9", interval=0, max_retries=1, backoff_base=0.01, timeout=(0.5, 0.5)) as client:
            with pytest.raises(FetchError):
                client.get("/a.html")

    def test_timeout_is_retried(self, server):
        server.route("/a.html", [Reply(200, b"slow", delay=1.0), Reply(200, b"fast")])
        with make_client(server, timeout=(1.0, 0.3)) as client:
            assert client.get("/a.html").body == b"fast"

    def test_retry_after_parsing(self):
        now = datetime(2026, 9, 27, tzinfo=timezone.utc)
        later = (now + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert retry_after_seconds("7") == 7.0
        assert retry_after_seconds(later, now) == pytest.approx(30)
        assert retry_after_seconds("soon") is None
        assert retry_after_seconds(None) is None


class TestRedirects:
    def test_same_origin_followed(self, server):
        server.route("/old.html", Reply(301, headers={"Location": "/new.html"}))
        server.route("/new.html", Reply(200, b"new"))
        with make_client(server) as client:
            reply = client.get("/old.html")
        assert (reply.status, reply.body, reply.final_url) == (200, b"new", f"{server.base_url}/new.html")

    def test_leaving_the_origin_is_refused(self, server):
        elsewhere = server.base_url.replace("127.0.0.1", "localhost") + "/x.html"
        server.route("/old.html", Reply(302, headers={"Location": elsewhere}))
        with make_client(server) as client:
            reply = client.get("/old.html")
        assert (reply.status, reply.redirect_refused) == (302, elsewhere)
        assert server.hits("/x.html") == []

    def test_https_to_http_is_different_origin(self):
        client = Client("https://jam-capture-unisonleague-ww.ateamid.com")
        assert client.same_origin("https://jam-capture-unisonleague-ww.ateamid.com/en/a.html")
        assert not client.same_origin("http://jam-capture-unisonleague-ww.ateamid.com/en/not_found.html")
        assert not client.same_origin("https://example.com/en/a.html")

    def test_redirect_loop_is_bounded(self, server):
        server.route("/loop.html", Reply(302, headers={"Location": "/loop.html"}))
        with make_client(server, max_redirects=3) as client:
            with pytest.raises(FetchError, match="redirects"):
                client.get("/loop.html")
        assert len(server.hits("/loop.html")) == 4


class TestStop:
    def test_stop_before_request_sends_nothing(self, server):
        with make_client(server) as client:
            client.stop.set()
            with pytest.raises(Stopped):
                client.get("/a.html")
        assert server.log == []

    def test_stop_during_throttle_wait_is_immediate(self, server):
        server.route("/a.html", Reply(200, b"x"))
        with make_client(server, interval=5.0) as client:
            client.get("/a.html")
            threading.Timer(0.05, client.stop.set).start()
            began = time.monotonic()
            with pytest.raises(Stopped):
                client.get("/a.html")
            assert time.monotonic() - began < 0.5
        assert len(server.hits("/a.html")) == 1

    def test_stop_mid_request_finishes_response(self, server):
        server.route("/a.html", Reply(200, b"x", delay=0.3))
        with make_client(server) as client:
            threading.Timer(0.1, client.stop.set).start()
            assert client.get("/a.html").body == b"x"
            with pytest.raises(Stopped):
                client.get("/a.html")
        assert len(server.hits("/a.html")) == 1

    def test_stop_during_backoff_is_immediate(self, server):
        server.route("/a.html", Reply(503))
        with make_client(server, backoff_base=5.0) as client:
            threading.Timer(0.1, client.stop.set).start()
            began = time.monotonic()
            with pytest.raises(Stopped):
                client.get("/a.html")
            assert time.monotonic() - began < 0.5
