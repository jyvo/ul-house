"""detail page to evo lineage"""
from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup

from ul_house.config import EQUIP_ID_RE, EVO_KIND, EVO_LINK_LABEL_SELECTOR, EVO_LINK_NAME_SELECTOR, EVO_LINK_RE
from ul_house.seed.store import Store


class LinkMarkupError(ValueError):
    """a lineage slot no longer matches with selector expectation"""


@dataclass(frozen=True)
class EvoLink:
    source_id: str
    kind: str           # reforge | awakening | enlightening
    side: str           # before | after
    target_id: str
    target_name: str


def read_links(html: str, source_id: str) -> list[EvoLink]:
    soup = BeautifulSoup(html, "lxml")
    links: dict[tuple[str, str], EvoLink] = {}
    for dt in soup.select(EVO_LINK_LABEL_SELECTOR):
        label = dt.get_text(" ", strip=True).casefold()
        match = EVO_LINK_RE.match(label)
        if match is None:
            raise LinkMarkupError(f"{source_id}: unknown lineage label {label!r}")
        dd = dt.find_next_sibling("dd")
        if dd is None:
            raise LinkMarkupError(f"{source_id}: {label!r} has no slot")

        anchor = dd.select_one("a[href]")
        if anchor is None:
            continue
        target = EQUIP_ID_RE.search(anchor["href"])
        if target is None:
            raise LinkMarkupError(f"{source_id}: {label!r} links to {anchor['href']!r}")
        if target.group(1) == source_id:
            raise LinkMarkupError(f"{source_id}: {label!r} links to itself")

        kind, side = EVO_KIND[match["kind"]], match["side"]
        if (kind, side) in links:
            raise LinkMarkupError(f"{source_id}: two {label!r} slots")
        name = dd.select_one(EVO_LINK_NAME_SELECTOR)
        links[(kind, side)] = EvoLink(source_id, kind, side, target.group(1),
                                      name.get_text(" ", strip=True) if name else "")
    return sorted(links.values(), key=lambda link: (link.kind, link.side))


def record_links(store: Store, source_id: str, html: str) -> list[EvoLink]:
    """read and store"""
    links = read_links(html, source_id)
    store.put_links(source_id, links)
    return links
