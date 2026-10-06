from __future__ import annotations

import math
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
    delays: dict[str, float] = field(default_factory=dict, compare=False)

    def allowed(self, path: str, agent: str = AGENT_TOKEN) -> bool:
        if self.state == "absent":
            return True
        if self.state == "unreachable" or self.parser is None:
            return False
        return self.parser.can_fetch(agent, path)

    def crawl_delay(self, agent: str = AGENT_TOKEN) -> float | None:
        """the group naming this agent (robotparser's substring rule), else the * group"""
        token = agent.casefold()
        for name, delay in self.delays.items():
            if name != "*" and name in token:
                return delay
        return self.delays.get("*")


def _crawl_delays(lines: list[str]) -> dict[str, float]:
    delays: dict[str, float] = {}
    agents: list[str] = []
    in_rules = False
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        name = name.casefold()
        if name == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.casefold())
            continue
        in_rules = True
        if name == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                continue
            if math.isfinite(delay) and delay >= 0:
                for agent in agents:
                    delays.setdefault(agent, delay)
    return delays


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
    if reply.status == 429 or reply.status >= 500:
        return Robots("unreachable", reply.status)
    if 400 <= reply.status < 500:
        return Robots("absent", reply.status)
    if reply.status != 200 or _detect_html(reply.body, reply.content_type):
        return Robots("absent", reply.status)

    lines = (reply.body or b"").decode("utf-8-sig", errors="replace").splitlines()
    parser = RobotFileParser()
    parser.parse(lines)
    return Robots("rules", reply.status, parser, _crawl_delays(lines))


def apply(robots: Robots, client: Client) -> None:
    """Crawl-delay may only slow the client down"""
    delay = robots.crawl_delay()
    if delay is not None:
        client.throttle.widen(delay)
