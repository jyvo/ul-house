import logging
import re
from bs4 import BeautifulSoup, Tag
from config import BASE_URL, NAME_SELECTOR, BASIC_INFO_SELECTOR, STATS_NAME_SELECTOR, STATS_SELECTOR, STATS_VAL_SELECTOR, SKILLS_SELECTOR, WEAPON_ABILITY_SELECTOR, SP_EVO_SELECTOR, SP_EVO_MAT_TITLE_SELECTOR, SP_EVO_MAT_CONTENT_CLASSNAME, SP_EVO_MAT_BLOCK_SELECTOR

log = logging.getLogger("parser")


_QUANTITY_RE = re.compile(r"^(?P<name>.*?)\s*×\s*(?P<quantity>[\d,]+)\s*$")
_POTENTIAL_RE = re.compile(r"Lv\s*(\d+)\s*Effect", re.IGNORECASE)
_LAZY_SRC = ("data-src", "src")
_MATERIAL_KINDS = {"Materials Needed (Gear)": "gear", "Materials Needed (Items)": "item"}
_HEADING_CLASS_RE = re.compile("ttl|title|head")
_DETAIL_ID_RE = re.compile(r"equip_detail/(\d+)\.html")

# (str, str|None, str|None, str|None, str|None) 
SKILL_SLOTS = ("main skill", "main skill 2", "passive skill", "hidden potential", "awakening restrictions")
MAIN, MAIN2, PASSIVE, POTENTIAL, RESTRICTIONS = 0, 1, 2, 3, 4

def detail_url(item_id: str) -> str:
    return f"{BASE_URL}/en/equip_detail/{item_id}.html"

def icon_url(item_id: str) -> str:
    return f"{BASE_URL}/images/equipicon/{item_id}.png"

# net for catching empty node
def _text(node: Tag | None) -> str:
    return node.get_text(" ", strip=True) if node is not None else ""

# dt-dd sibling pairs
def _pairs(block: Tag):
    out: dict[str, str] = {}
    for dt in block.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None:
            out[dt.get_text(strip=True)] = dd.get_text("\n", strip=True)
    return out

def _lazy_url(img: Tag | None) -> str | None:
    if img is None:
        return None

    for attr in _LAZY_SRC:
        value = img.get(attr)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None

def _heading(block: Tag):
    heading = block.find_previous(["h2", "h3", "p", "div"], class_=_HEADING_CLASS_RE)
    return _text(heading)

def _parse_name(soup: BeautifulSoup):
    return _text(soup.select_one(NAME_SELECTOR))

def _parse_data(soup: BeautifulSoup):
    return {
        _text(dl.find("dt")): _text(dl.find("dd"))
        for dl in soup.select(BASIC_INFO_SELECTOR)
        if _text(dl.find("dt"))
    }

def _parse_stats(soup: BeautifulSoup):
    stats_name = [dd.get_text(strip=True) for dd in soup.select(STATS_NAME_SELECTOR)[1:]]
    stats_val = []
    for dl in soup.select_one(STATS_SELECTOR).select(STATS_VAL_SELECTOR):
        col = [dd.get_text(strip=True) for dd in dl.select("dd")]
        stats_val.append(col)

    return {
        stat: {row[0]: row[i] for row in stats_val if row[i] != "-"}
        for i, stat in enumerate(stats_name, start=1)
        if any(row[i] != "-" for row in stats_val)
    }

def _parse_skills(soup: BeautifulSoup):
    slots: list[object] = [None] * len(SKILL_SLOTS)
    potential: dict[str, str] = {}

    for div in soup.select(SKILLS_SELECTOR):
        dl = div.select_one("dl.detail__evo")
        if dl is None:
            continue
        fields = _pairs(dl)
        heading = _heading(div)

        if "Skill Name" in fields:
            skill = {
                "name": fields.get("Skill Name"),
                "effect": fields.get("Effect", ""),
                "heading": heading,
            }
            if "passive" in heading.casefold():
                index = PASSIVE
            else:
                index = MAIN if slots[MAIN] is None else MAIN2

            if slots[index] is not None:
                log.warning("more than one %s block; keeping the first", SKILL_SLOTS[index])
                continue
            slots[index] = skill
            continue

        if "Restrictions" in fields:
            slots[RESTRICTIONS] = fields["Restrictions"]
            continue

        for label, value in fields.items():
            if (match := _POTENTIAL_RE.fullmatch(label)) and value:
                potential[f"Lv{match.group(1)}"] = value

    slots[POTENTIAL] = potential or None
    return slots

def _parse_ability(soup: BeautifulSoup):
    ability: dict[str, str] = {}
    for dl in soup.select(WEAPON_ABILITY_SELECTOR):
        ability.update(_pairs(dl))
    return ability

def _parse_reforge(soup: BeautifulSoup):
    for dl in soup.select("dl.detail__evo"):
        labels = {dt.get_text(strip=True) for dt in dl.find_all("dt")}
        if not labels & {"Before Reforging", "After Reforging"}:
            continue
        found: dict[str, str | None] = {}
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            match = _DETAIL_ID_RE.search(str(dd)) if dd is not None else None
            found[_text(dt)] = match.group(1) if match else None
        return {
            "before": found.get("Before Reforging"),
            "after": found.get("After Reforging"),
        }
    return {"before": None, "after": None}


def _parse_awakening(soup: BeautifulSoup):
    lists: dict[str, list[dict[str, object]]] = {}
    for title in soup.select(SP_EVO_MAT_TITLE_SELECTOR):
        kind = _MATERIAL_KINDS.get(_text(title))
        if kind is None:
            continue
        contents = title.find_next_sibling("div", class_=SP_EVO_MAT_CONTENT_CLASSNAME)
        if contents is None:
            continue

        entries: list[dict[str, object]] = []
        for cell in contents.select(SP_EVO_MAT_BLOCK_SELECTOR):
            label = _text(cell.find("p"))
            if not label:
                continue

            if match:= _QUANTITY_RE.match(label):
                name, quantity = match.group("name"), int(match.group("quantity").replace(",", ""))
            else:
                name, quantity = label, 1

            link = cell.find("a", href=_DETAIL_ID_RE)
            icon = _lazy_url(cell.select_one("img.sp_icon"))
            source = _DETAIL_ID_RE.search(str(link["href"])) if link is not None else None
            if source is None and icon:
                source = re.search(r"/(?:item_)?(\d+)\.png", icon)
            entries.append(
                {
                    "name": name,
                    "quantity": quantity,
                    "source": source.group(1) if source else None,
                    "icon": icon,
                    }
            )
        lists[kind] = entries
    return lists

def _sp_evo_title(soup: BeautifulSoup):
    title = soup.select_one(SP_EVO_SELECTOR)
    return _text(title)
