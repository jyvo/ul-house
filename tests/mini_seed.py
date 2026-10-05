"""mini seedm, real crawler run against local fake wiki serving cached detail pages"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from html import escape
from pathlib import Path

import pytest

from fetch_fixtures import cached_path, missing
from local_server import LocalServer, Reply
from ul_house.config import LIST_SOURCES, NEW_RELEASE_PATH, detail_path
from ul_house.crawl.run import Lineage, crawl
from ul_house.http.client import Client
from ul_house.seed.store import Store
from ul_house.settings import Scope, load_scope

MINI_CLOCK = "2026-10-04T23:17:00Z"
MINI_LINEAGE = Lineage(code_commit="mini-seed", catalog_commit="mini-seed")
DEFAULT_ICON = object()

ROBOTS_PATH = "/robots.txt"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def mini_scope() -> Scope:
    """the shipped catalog.toml with cost.min = 35 (for 4435013)"""
    data = load_scope().to_mapping()
    data["cost"]["min"] = 35
    return Scope.from_mapping(data)


@dataclass
class WikiItem:
    item_id: str
    name: str
    grp: str                                            # weapon | armor | monster
    rarity: str
    cost: int
    gear_type: str | None = "1"
    element: str = "1"
    links: tuple[tuple[str, str, str, str], ...] = ()   # (side, printed kind, target_id, target_name)
    listed: bool = True
    detail: bool = True
    html: bytes | None = None                           # real page bytes | None = synthetic page
    icon: bytes | None | object = DEFAULT_ICON

    def __post_init__(self):
        if self.icon is DEFAULT_ICON:
            self.icon = fake_png(self.item_id)


def fake_png(item_id: str, version: int = 1) -> bytes:
    return PNG_MAGIC + f"{item_id}:{version}".encode()


def icon_path(item_id: str) -> str:
    return f"/images/equipicon/{item_id}.png"


def _slot(label: str, target_id: str, target_name: str) -> str:
    return (f'<dt class="detail__evo--last"><span>{escape(label)}</span></dt>'
            f'<dd class="detail__evo--last"><a href="/en/equip_detail/{target_id}.html">'
            f'<div class="detail__evo--block"><img class="icon lazyload" data-src="{icon_path(target_id)}"/></div>'
            f'<div class="detail__evo--block detail__evo--link"><p class="evo_name">{escape(target_name)}</p></div>'
            f"</a></dd>")


def detail_html(item: WikiItem) -> str:
    slots = "".join(_slot(f"{side.title()} {printed.title()}", target_id, target_name)
                    for side, printed, target_id, target_name in item.links)
    return ("<html><head><title>Limipedia</title></head><body>"
            '<p class="title_bar--text">Basic Info</p>'
            f'<p class="name__text">{escape(item.name)}</p>'
            f'<dl class="detail__evo">{slots}</dl>'
            "</body></html>")


def _cell(item: WikiItem) -> str:
    inputs = []
    if item.gear_type is not None:
        inputs.append(f'<input name="unisonleague_type" type="hidden" value="{item.gear_type}"/>')
    inputs.append(f'<input name="unisonleague_attribute" type="hidden" value="{item.element}"/>')
    inputs.append(f'<input name="unisonleague_cost" type="hidden" value="{item.cost}"/>')
    inputs.append('<input name="unisonleague_max_refining_count" type="hidden" value="5"/>')
    return (f'<td class="filter">{"".join(inputs)}<a href="/en/equip_detail/{item.item_id}.html">'
            f'<div class="icons"><img class="icon lazyload" data-src="{icon_path(item.item_id)}"/></div>'
            f'<p class="list_item_name">{escape(item.name)}</p></a></td>')


def list_html(items: Iterable[WikiItem]) -> str:
    cells = "".join(_cell(item) for item in items)
    return f'<html><body><table class="main"><tbody><tr>{cells}</tr></tbody></table></body></html>'


def release_html(pairs: Iterable[tuple[str, str]]) -> str:
    tds = "".join(f'<td><a href="equip_detail/{uid}.html"><p class="list_item_name">{escape(name)}</p></a></td>'
                  for uid, name in pairs)
    return f'<html><body><table class="main"><tbody><tr>{tds}</tr></tbody></table></body></html>'


def _conditional(body: bytes, content_type: str):
    etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'

    def reply(request):
        if request.headers.get("If-None-Match") == etag:
            return Reply(304, headers={"ETag": etag})
        return Reply(200, body, {"ETag": etag, "Content-Type": content_type})
    return reply


HTML = "text/html; charset=utf-8"


class FakeWiki:
    def __init__(self, items: Iterable[WikiItem] = (), releases: Iterable[tuple[str, str]] = ()):
        self.items: dict[str, WikiItem] = {item.item_id: item for item in items}
        self.releases: list[tuple[str, str]] = list(releases)
        self.robots: Reply | None = None       # None: /robots.txt answers 404 (absent)

    def _source_index(self, item: WikiItem) -> int:
        for index, (_, grp, rarity) in enumerate(LIST_SOURCES):
            if (grp, rarity) == (item.grp, item.rarity):
                return index
        raise ValueError(f"no list page for {item.grp} {item.rarity}")

    def install(self, server: LocalServer) -> None:
        if self.robots is None:
            server.routes.pop(ROBOTS_PATH, None)
        else:
            server.route(ROBOTS_PATH, self.robots)

        for index, (path, _, _) in enumerate(LIST_SOURCES):
            rows = [item for item in self.items.values() if item.listed and self._source_index(item) == index]
            if not rows:       # filler: cosmetic cost, so select() always drops it
                rows = [WikiItem(f"99{index}00001", "filler", "weapon", "UR", 99)]
            server.route(path, _conditional(list_html(rows).encode(), HTML))
        server.route(NEW_RELEASE_PATH, _conditional(release_html(self.releases).encode(), HTML))

        for item in self.items.values():
            if item.detail:
                body = item.html if item.html is not None else detail_html(item).encode()
                server.route(detail_path(item.item_id), _conditional(body, HTML))
            else:
                server.routes.pop(detail_path(item.item_id), None)
            if isinstance(item.icon, bytes):
                server.route(icon_path(item.item_id), _conditional(item.icon, "image/png"))
            else:
                server.routes.pop(icon_path(item.item_id), None)


# cached pages
MINI_ITEMS = (
    WikiItem("1015655", "[Surging Sea] Sea Dragon's Sword", "weapon", "UR", 44),
    WikiItem("1015157", "Absolute Xenoblade", "weapon", "UR", 40),
    WikiItem("1890424", "[Oblivion] John Doe's Hat (F)", "armor", "UR", 45),
    WikiItem("1796604", "[Eclipse's Hope] P. Valkyrie Nikyt", "monster", "UR", 45, gear_type=None),
    WikiItem("1500502", "[Raging Howl Fatewoven] Fermuraze", "monster", "UR", 45, gear_type=None),
    WikiItem("4425111", "[Soldier Founder] Odinia", "monster", "UR", 43, gear_type=None),
    WikiItem("4435013", "Oberon, Sky Emperor", "monster", "UR", 35, gear_type=None),
    WikiItem("4434015", "Oberon", "monster", "SSR", 30, gear_type=None),
)
UNLISTED_RELEASE = ("1999999", "Unlisted Newcomer")


def _skip_if_missing() -> None:
    absent = missing()
    if absent:
        pytest.skip(f"fixture pages not cached: {', '.join(absent)}")


def mini_wiki() -> FakeWiki:
    _skip_if_missing()
    items = [replace(item, html=cached_path(item.item_id).read_bytes()) for item in MINI_ITEMS]
    releases = [(item.item_id, item.name) for item in items] + [UNLISTED_RELEASE]
    return FakeWiki(items, releases)


def build_mini_seed(path: Path) -> Path:
    """run the real crawler once against mini_wiki()"""
    _skip_if_missing()
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    wiki = mini_wiki()
    with LocalServer() as server:
        wiki.install(server)
        with Client(server.base_url, interval=0.0, max_retries=0, timeout=(2, 5)) as client:
            with Store.open(path, clock=lambda: MINI_CLOCK) as store:
                crawl(client, store, mini_scope(), lineage=MINI_LINEAGE, trigger="manual")
    return path
