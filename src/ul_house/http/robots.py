from __future__ import annotations

from dataclasses import dataclass, field
from urllib.robotparser import RobotFileParser

from ul_house.config import USER_AGENT
from ul_house.http.client import Client, FetchError

ROBOTS_PATH = "/robots.txt"
AGENT_TOKEN = USER_AGENT.split("/", 1)[0]       # product token robots rules are matched against


@dataclass(frozen=True)
class Robots:
    state: str                                   # rules | absent | unreachable
    status: int | None = None
    parser: RobotFileParser | None = field(default=None, compare=False)

    def allowed(self, path: str, agent: str = AGENT_TOKEN) -> bool:
        if self.state == "absent":
            return True
        if self.state == "unreachable" or self.parser is None:
            return False
        return self.parser.can_fetch(agent, path)

    def crawl_delay(self, agent: str = AGENT_TOKEN) -> float | None:
        if self.parser is None:
            return None
        delay = self.parser.crawl_delay(agent)
        return float(delay) if delay is not None else None


def _detect_html(body: bytes | None, content_type: str | None) -> bool:
    if content_type and "html" in content_type.lower():
        return True
    head = (body or b"")[:512].lstrip().lower()
    return head.startswith((b"<!doctype", b"<html"))


def load(client: Client) -> Robots:
    try:
        reply = client.get(ROBOTS_PATH)
    except FetchError:
        return Robots("unreachable")

    if reply.redirect_refused is not None:
        return Robots("absent", reply.status)
    if 400 <= reply.status < 500:
        return Robots("absent", reply.status)
    if reply.status >= 500:
        return Robots("unreachable", reply.status)
    if reply.status != 200 or _detect_html(reply.body, reply.content_type):
        return Robots("absent", reply.status)

    parser = RobotFileParser()
    parser.parse((reply.body or b"").decode("utf-8", errors="replace").splitlines())
    return Robots("rules", reply.status, parser)


def apply(robots: Robots, client: Client) -> None:
    """Crawl-delay may only slow the client down"""
    delay = robots.crawl_delay()
    if delay is not None:
        client.throttle.widen(delay)
