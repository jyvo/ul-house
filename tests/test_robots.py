import pytest

from local_server import LocalServer, Reply
from ul_house.http import robots
from ul_house.http.client import Client


@pytest.fixture
def server():
    with LocalServer() as s:
        yield s


def client_for(server, **kw):
    return Client(server.base_url, interval=0.0, max_retries=1, backoff_base=0.01, timeout=(0.5, 0.5), **kw)


def text(body: str, **headers):
    return Reply(200, body.encode(), {"Content-Type": "text/plain", **headers})


class TestOutcomes:
    def test_absent_404(self, server):
        with client_for(server) as client:
            gate = robots.load(client)
        assert (gate.state, gate.status) == ("absent", 404)
        assert gate.allowed("/en/equip_detail/1.html")

    def test_soft_404_redirect(self, server):
        """301 > http:// not-found page"""
        elsewhere = server.base_url.replace("127.0.0.1", "localhost") + "/en/not_found.html"
        server.route("/robots.txt", Reply(301, headers={"Location": elsewhere}))
        with client_for(server) as client:
            gate = robots.load(client)
        assert gate.state == "absent"
        assert gate.allowed("/en/equip_list/1_5.html")

    def test_soft_404_html_body(self, server):
        server.route("/robots.txt", Reply(301, headers={"Location": "/en/not_found.html"}))
        server.route("/en/not_found.html", Reply(200, b"<!DOCTYPE html><html>not found</html>",
                                                 {"Content-Type": "text/html; charset=utf-8"}))
        with client_for(server) as client:
            assert robots.load(client).state == "absent"

    def test_html_body_mislabelled_as_text(self, server):
        server.route("/robots.txt", text("<html><body>Disallow: /</body></html>"))
        with client_for(server) as client:
            assert robots.load(client).state == "absent"

    def test_unreachable_5xx(self, server):
        # disallows everything
        server.route("/robots.txt", Reply(503))
        with client_for(server) as client:
            gate = robots.load(client)
        assert gate.state == "unreachable"
        assert not gate.allowed("/en/equip_detail/1.html")

    def test_unreachable_transport(self):
        with Client("http://127.0.0.1:9", interval=0, max_retries=0, timeout=(0.5, 0.5)) as client:
            assert robots.load(client).state == "unreachable"

    def test_permissive_rules(self, server):
        server.route("/robots.txt", text("User-agent: *\nDisallow:\n"))
        with client_for(server) as client:
            gate = robots.load(client)
        assert gate.state == "rules"
        assert gate.allowed("/en/equip_detail/1.html")

    def test_restrictive_token_rules(self, server):
        server.route("/robots.txt", text(
            "User-agent: ul-house\nDisallow: /en/equip_detail/\n\nUser-agent: *\nDisallow:\n"))
        with client_for(server) as client:
            gate = robots.load(client)
        assert not gate.allowed("/en/equip_detail/1.html")
        assert gate.allowed("/en/equip_list/1_5.html")


class TestCrawlDelay:
    def test_delay_widens_throttle(self, server):
        server.route("/robots.txt", text("User-agent: *\nCrawl-delay: 2\n"))
        with client_for(server) as client:
            robots.apply(robots.load(client), client)
            assert client.throttle.interval == 2.0

    def test_delay_never_speeds_up(self, server):
        server.route("/robots.txt", text("User-agent: *\nCrawl-delay: 0\n"))
        with Client(server.base_url, interval=0.15) as client:
            robots.apply(robots.load(client), client)
            assert client.throttle.interval == 0.15

    def test_no_delay_when_absent(self, server):
        with client_for(server) as client:
            robots.apply(robots.load(client), client)
            assert client.throttle.interval == 0.0
