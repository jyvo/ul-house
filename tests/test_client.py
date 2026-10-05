import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

from local_server import LocalServer, Reply, RawServer
from ul_house.config import USER_AGENT
from ul_house.http.client import RETRY_STATUSES, Client, FetchError, Stopped, Throttle, retry_after_seconds


OK_BODY = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
SHORT_BODY = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\nonly-ten!!"
CUT_CHUNKED = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\nhello\r\n"


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


class TestResponseHeaders:
    def test_headers_are_case_insensitive_on_every_status(self, server):
        server.route("/a.html", Reply(200, b"ok", {"X-API-Commit": "abc123"}))
        server.route("/n.html", Reply(304, headers={"X-UL-To-Revision": "7"}))
        server.route("/t.html", Reply(429, headers={"X-API-Commit": "def"}))
        with make_client(server, max_retries=0) as client:
            ok = client.get("/a.html")
            assert ok.headers["x-api-commit"] == "abc123" and ok.headers["X-API-Commit"] == "abc123"
            assert client.get("/n.html").headers["x-ul-to-revision"] == "7"
            assert client.get("/t.html").headers.get("X-Api-Commit") == "def"

    def test_headers_come_from_the_final_hop(self, server):
        server.route("/r.html", Reply(302, headers={"Location": "/b.html", "X-API-Commit": "first"}))
        server.route("/b.html", Reply(200, b"ok", {"X-API-Commit": "last"}))
        with make_client(server) as client:
            assert client.get("/r.html").headers["x-api-commit"] == "last"


class TestResponseHeadersDefault:
    def test_headers_default_to_empty_and_do_not_affect_equality(self):
        from ul_house.http.client import Response

        fields = dict(path="/a.html", status=200, body=b"x", etag=None, last_modified=None,
                      content_type=None, final_url="http://h/a.html")
        bare = Response(**fields)
        assert len(bare.headers) == 0 and bare.headers.get("X-Anything") is None
        assert bare == Response(**fields, headers={"X-API-Commit": "abc"})


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

    def test_retry_statuses_can_exclude_429(self, server):
        server.route("/a.html", [Reply(429, headers={"Retry-After": "0"}), Reply(200, b"ok")])
        with make_client(server, max_retries=2, backoff_base=0, retry_statuses=RETRY_STATUSES - {429}) as client:
            assert client.get("/a.html").status == 429
        assert len(server.hits("/a.html")) == 1

        server.route("/b.html", [Reply(429, headers={"Retry-After": "0"}), Reply(200, b"ok")])
        with make_client(server, max_retries=2, backoff_base=0) as client:
            assert client.get("/b.html").status == 200
        assert len(server.hits("/b.html")) == 2

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


def raw_client(server, **kw):
    kw.setdefault("interval", 0.0)
    kw.setdefault("backoff_base", 0.01)
    kw.setdefault("timeout", (1.0, 1.0))
    return Client(server.base_url, **kw)


class TestTruncatedBodies:
    """body cut off mid transfer"""

    @pytest.mark.parametrize("broken", [SHORT_BODY, CUT_CHUNKED], ids=["short-content-length", "cut-chunked"])
    def test_retried_then_succeeds(self, broken):
        with RawServer([broken, OK_BODY]) as server, raw_client(server) as client:
            assert client.get("/x").body == b"ok"
            assert server.connections == 2

    @pytest.mark.parametrize("broken", [SHORT_BODY, CUT_CHUNKED], ids=["short-content-length", "cut-chunked"])
    def test_exhausted_is_a_fetch_error(self, broken):
        with RawServer([broken]) as server, raw_client(server, max_retries=1) as client:
            with pytest.raises(FetchError):
                client.get("/x")
            assert server.connections == 2


class TestDeadline:
    """read timeout is per-read (deadline is total allowance)"""

    DRIP = (b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n", [b"x"] * 5, 0.2)

    def test_slow_drip_is_cut_off(self):
        """every read is well inside the 1 s read timeout; only the deadline can stop it"""
        with RawServer([self.DRIP]) as server, raw_client(server, max_retries=0, deadline=0.3) as client:
            began = time.monotonic()
            with pytest.raises(FetchError, match="DeadlineExceeded"):
                client.get("/x")
            assert time.monotonic() - began < 0.8      # the drip alone would take 1.0 s

    def test_drip_within_the_deadline_is_fine(self):
        with RawServer([self.DRIP]) as server, raw_client(server, deadline=10) as client:
            assert client.get("/x").body == b"x" * 5

    def test_deadline_must_be_positive(self):
        with pytest.raises(ValueError):
            Client("http://127.0.0.1:9", deadline=0)


class TestRetryAfterWithoutZone:
    """HTTP-date with no zone is read as GMT instead of raising TypeError"""

    def test_naive_date(self):
        now = datetime(2026, 9, 30, 10, 0, 0, tzinfo=timezone.utc)
        assert retry_after_seconds("Wed, 30 Sep 2026 10:00:30", now) == pytest.approx(30)

    def test_naive_date_in_a_live_retry(self, server):
        server.route("/a.html", [Reply(503, headers={"Retry-After": "Wed, 30 Sep 2026 10:00:00"}), Reply(200, b"ok")])
        with make_client(server) as client:
            assert client.get("/a.html").body == b"ok"


class TestRedirectEdges:
    """validators stay with the url they describe"""

    def test_conditional_headers_are_not_forwarded(self, server):
        server.route("/old.html", Reply(301, headers={"Location": "/new.html"}))
        server.route("/new.html", Reply(200, b"new"))
        with make_client(server) as client:
            client.get("/old.html", {"If-None-Match": '"v1"', "If-Modified-Since": "Mon"})
        (old,), (new,) = server.hits("/old.html"), server.hits("/new.html")
        assert old.headers.get("If-None-Match") == '"v1"'
        assert "If-None-Match" not in new.headers and "If-Modified-Since" not in new.headers

    def test_explicit_default_port_is_the_same_origin(self):
        client = Client("https://jam-capture-unisonleague-ww.ateamid.com")
        assert client.same_origin("https://jam-capture-unisonleague-ww.ateamid.com:443/en/a.html")
        assert client.same_origin("HTTPS://JAM-CAPTURE-UNISONLEAGUE-WW.ATEAMID.COM/en/a.html")
        assert not client.same_origin("https://jam-capture-unisonleague-ww.ateamid.com:8443/en/a.html")
        assert not client.same_origin("http://jam-capture-unisonleague-ww.ateamid.com:443/en/a.html")


class TestBodyDecoding:
    """reading the body by hand must match decoded requests"""

    def test_gzip_body_is_decoded(self, server):
        import gzip

        page = b"<html>" + b"row " * 20000 + b"</html>"
        server.route("/g.html", Reply(200, gzip.compress(page), {"Content-Encoding": "gzip"}))
        with make_client(server) as client:
            assert client.get("/g.html").body == page

    def test_empty_200_is_empty_bytes(self, server):
        server.route("/e.html", Reply(200, b""))
        with make_client(server) as client:
            assert client.get("/e.html").body == b""
