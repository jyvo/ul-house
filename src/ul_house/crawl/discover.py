"""list pages > ListRow records
    - row reading not parsed
    - carries id, name, cost
    - rarity come from page group (not rows)
    - new release page is different
        - rows carry only id and name (no cost, no group) > cannot feed select()
        - ids cross checked against listing instead
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ul_house.config import (
    EQUIP_ID_RE,
    LIST_ROW_INPUT,
    LIST_ROW_NAME_SELECTOR,
    LIST_ROW_SELECTOR,
    LIST_SOURCES,
    NEW_RELEASE_PATH,
)
from ul_house.http.client import Client, FetchError
from ul_house.seed.store import Store


class ListMarkupError(ValueError):
    """a list page no longer looks as the selectors expected"""


class DiscoveryError(RuntimeError):
    """a list page could not be read; listing is left exactly as it was"""


@dataclass(frozen=True)
class ListSource:
    path: str
    grp: str
    rarity: str


SOURCES = tuple(ListSource(*source) for source in LIST_SOURCES)


@dataclass(frozen=True)
class ListRow:
    item_id: str
    name: str
    grp: str
    rarity: str
    gear_type: str | None
    element: str
    cost: int
    source_url: str

    @classmethod
    def from_db(cls, row: sqlite3.Row) -> ListRow:
        return cls(row["item_id"], row["name"], row["grp"], row["rarity"], row["gear_type"],
                   row["element"], row["cost"], row["source_url"])


@dataclass(frozen=True)
class ReleaseRow:
    item_id: str
    name: str


def _input(td, name: str) -> str | None:
    node = td.select_one(LIST_ROW_INPUT.format(field=name))
    value = node.get("value") if node is not None else None
    return value.strip() if isinstance(value, str) else None


def read_rows(html: str, source: ListSource) -> list[ListRow]:
    soup = BeautifulSoup(html, "lxml")
    cells = soup.select(LIST_ROW_SELECTOR)
    if not cells:
        raise ListMarkupError(f"{source.path}: no {LIST_ROW_SELECTOR!r} rows")

    rows: dict[str, ListRow] = {}
    for idx, td in enumerate(cells):
        where = f"{source.path} row {idx}"
        anchor = td.select_one("a[href]")
        match = EQUIP_ID_RE.search(anchor["href"]) if anchor is not None else None
        name = td.select_one(LIST_ROW_NAME_SELECTOR)
        cost, element = _input(td, "cost"), _input(td, "attribute")
        if match is None:
            raise ListMarkupError(f"{where}: no equip_detail link")
        if name is None or not name.get_text(strip=True):
            raise ListMarkupError(f"{where}: no name")
        if cost is None or not cost.isdigit():
            raise ListMarkupError(f"{where}: cost is {cost!r}")
        if element is None:
            raise ListMarkupError(f"{where}: no attribute")

        item_id = match.group(1)
        rows.setdefault(item_id, ListRow(
            item_id=item_id,
            name=name.get_text(" ", strip=True),
            grp=source.grp,
            rarity=source.rarity,
            gear_type=_input(td, "type"),
            element=element,
            cost=int(cost),
            source_url=source.path,
        ))
    return list(rows.values())


def read_new_release(html: str) -> list[ReleaseRow]:
    soup = BeautifulSoup(html, "lxml")
    seen: dict[str, ReleaseRow] = {}
    for anchor in soup.select("a[href]"):
        match = EQUIP_ID_RE.search(anchor["href"])
        if match is None:
            continue
        name = anchor.select_one(LIST_ROW_NAME_SELECTOR)
        seen.setdefault(match.group(1), ReleaseRow(match.group(1), name.get_text(" ", strip=True) if name else ""))
    if not seen:
        raise ListMarkupError(f"{NEW_RELEASE_PATH}: no equip_detail links")
    return list(seen.values())


@dataclass
class Discovery:
    rows: list[ListRow] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    vanished: list[str] = field(default_factory=list)
    unlisted_releases: list[ReleaseRow] = field(default_factory=list)
    new_release_error: str | None = None


def _fetch_page(client: Client, store: Store, path: str, kind: str,
                run_id: int | None = None) -> tuple[str, bool]:
    """conditional GET: returns (html, changed) > raises anything but 200/304 (transaction rolls back)"""
    validators = store.validators(path)
    reply = client.get(path, validators.headers() if validators else None)
    if reply.status == 200:
        changed = store.record_response(path, kind, 200, reply.body, reply.etag, reply.last_modified,
                                        reply.content_type, run_id=run_id)
        return reply.body.decode("utf-8", errors="replace"), changed
    if reply.status == 304 and validators is not None:
        store.record_response(path, kind, 304, etag=reply.etag, last_modified=reply.last_modified, run_id=run_id)
        return store.html(path), False
    detail = f" (redirect to {reply.redirect_refused} refused)" if reply.redirect_refused else ""
    raise DiscoveryError(f"{path}: HTTP {reply.status}{detail}")


def discover(client: Client, store: Store, sources: tuple[ListSource, ...] = SOURCES, new_release: bool = True,
             *, run_id: int | None = None) -> Discovery:
    """read list pages into listing"""
    result = Discovery()
    for source in sources:
        with store.transaction():
            html, changed = _fetch_page(client, store, source.path, "list", run_id)
            rows = read_rows(html, source)
            fresh = {row.item_id for row in rows}
            before = {r["item_id"] for r in store.conn.execute(
                "SELECT item_id FROM listing WHERE source_url = ?", (source.path,))}
            store.put_listing(rows)
        result.rows.extend(rows)
        result.vanished.extend(sorted(before - fresh))
        if changed:
            result.changed.append(source.path)

    if new_release:
        try:
            with store.transaction():
                html, changed = _fetch_page(client, store, NEW_RELEASE_PATH, "new_release", run_id)
                releases = read_new_release(html)
        except (DiscoveryError, ListMarkupError, FetchError) as exc:
            result.new_release_error = f"{type(exc).__name__}: {exc}"
        else:
            if changed:
                result.changed.append(NEW_RELEASE_PATH)
            listed = {r["item_id"] for r in store.conn.execute("SELECT item_id FROM listing")}
            result.unlisted_releases = [row for row in releases if row.item_id not in listed]
    return result
