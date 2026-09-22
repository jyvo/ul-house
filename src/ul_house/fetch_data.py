import re
from bs4 import BeautifulSoup, Tag
from ul_house.config import BASE_URL, BASIC_DATA_SELECTOR, EQUIP_REF_TAG, HEADING_SELECTOR, ITEM_REF_TAG, NAME_SELECTOR, REFORGE_MAT_SELECTOR, REFORGE_SELECTOR, SP_MAT_CONTENT_NAME, SP_MAT_CONTENT_SELECTOR, SP_MAT_TITLE_SELECTOR, STATS_NAME_SELECTOR, STATS_SELECTOR, SKILLS_SELECTOR, WEAPON_ABILITY_SELECTOR
from ul_house.config import _EQUIP_ID_RE, _ITEM_ID_RE, _ABILITY_ID_RE


def detail_url(equip_id: str) -> str:
    return f"{BASE_URL}/en/equip_detail/{equip_id}.html"


def _text(node: Tag | None) -> str:
    return node.get_text(" ", strip=True).casefold() if node is not None else ""


def _pairs(node: Tag, next_sib: str, **kwargs) -> tuple[str, Tag] | None:
    label = _text(node)
    content = node.find_next_sibling(next_sib)
    return label, content


def _match_re(regex: re.Pattern, node: Tag, attr: str) -> re.Match | None:
    return regex.search(node.get(attr)).group(1) if node else None


def _fetch_heading(node: Tag, selector: str) -> str | None:
    previous_node = node.find_previous_sibling(selector)
    return _text(previous_node.select_one(HEADING_SELECTOR)) if previous_node else None


def fetch_name(soup: BeautifulSoup) -> str:
    return _text(soup.select_one(NAME_SELECTOR))


def fetch_data(soup: BeautifulSoup) -> dict[str, str] | None:
    data = {}
    for dl in soup.select(BASIC_DATA_SELECTOR):
        label = _text(dl.find("dt"))
        value = _text(dl.find("dd"))
        data[label] = value
    return data or None


def fetch_stats(soup: BeautifulSoup) -> tuple[str, list[tuple[str, int]]] | None:
    stats = {}
    stat_labels = [_text(dd) for dd in soup.select(STATS_NAME_SELECTOR)[1:]]
    for dl in soup.select(STATS_SELECTOR)[1:]:
        tier = _text(dl.select_one("dd"))
        stat_col = dl.select("dd")[1:]
        
        for i, label in enumerate(stat_labels):
            value = _text(stat_col[i])
            if value != "-":
                stats.setdefault(label, []).append((tier, int(value.replace(",", ""))))

    return stats or None


def fetch_ability(soup: BeautifulSoup) -> dict[str, str] | None:
    info = {}
    for dt in soup.select(WEAPON_ABILITY_SELECTOR):
        label, content = _pairs(dt, "dd")
        if not label or not content:
            continue

        if label == "ability":
            info["name"] = _text(content)
            info["uid"] = _match_re(_ABILITY_ID_RE, content.select_one(EQUIP_REF_TAG[0]), EQUIP_REF_TAG[1])
        elif label == "effect":
            info["effect"] = _text(content)
    return info or None



def fetch_skills(soup: BeautifulSoup) -> dict[str, tuple[str, str]] | None:
    """fetches proc, mon skill 1+2, passive, hidden potential, restrictions
    returns : {heading: (skill_name, skill_effect)}
    """
    info = []
    current_skill = None

    for div in soup.select(SKILLS_SELECTOR[0]):
        heading = _fetch_heading(div, "div")
        if heading is None:
            continue

        entry = {}
        for dt in div.select(SKILLS_SELECTOR[1]):
            label, content = _pairs(dt, "dd")
            if not label or not content:
                continue

            if "potential" in heading:
                entry[label] = content.get_text(strip=True)
            else:
                if label == "skill name":
                    current_skill = _text(content)
                elif label == "effect" and current_skill is not None:
                    entry[heading] = (current_skill, _text(content))
                    current_skill = None
                elif "restrictions" in label:
                    entry[label] = _text(content)

        if entry:
            if "potential" in heading:
                info.append({heading: entry})
            else:
                info.append(entry)
    return info or None



def _fetch_evo(soup: BeautifulSoup, selector: str) -> list[dict[str, dict[str, str | int]]] | None:
    info = []
    for dl in soup.select(selector):

        heading = _fetch_heading(dl, "div")
        if not heading:
            continue
        
        entry = {}
        for dt in dl.select("dt"):
            label, content = _pairs(dt, "dd")
            if not label or not content:
                continue

            if "before" in label:
                entry["before"] = _text(content)
                equip_id = _match_re(_EQUIP_ID_RE, content.select_one(EQUIP_REF_TAG[0]), EQUIP_REF_TAG[1])
                if equip_id:
                    entry["before_id"] = equip_id
            elif "after" in label:
                entry["after"] = _text(content)
                equip_id = _match_re(_EQUIP_ID_RE, content.select_one(EQUIP_REF_TAG[0]), EQUIP_REF_TAG[1])
                if equip_id:
                    entry["after_id"] = equip_id
        if entry:
            info.append({heading: entry})
    return info or None


def _fetch_reforge_mats(soup: BeautifulSoup) -> dict[str, dict[str, str | int]] | None:
    """return : {ref_id: {name: str, quantity: int}}"""
    info = {}
    for dd in soup.select(REFORGE_MAT_SELECTOR):
        name = _text(dd)
        ref_id = _match_re(_EQUIP_ID_RE, dd.select_one(EQUIP_REF_TAG[0]), EQUIP_REF_TAG[1])
        if ref_id in info:
            info[ref_id]["quantity"] += 1
        else:
            info[ref_id] = {"name": name, "quantity": 1}
    return info if info else None


def fetch_reforge(soup: BeautifulSoup):
    return _fetch_evo(soup, REFORGE_SELECTOR), _fetch_reforge_mats(soup)


def _fetch_sp_materials(soup: BeautifulSoup) -> list[dict[str, dict[set[str, str]]]] | None:
    """return : [{heading: {ref_id: (name, quantity)}}]"""
    mats = []
    for div in soup.select(SP_MAT_TITLE_SELECTOR):
        heading, mats_div = _pairs(div, "div", _class=SP_MAT_CONTENT_NAME)

        entry = {}
        for td in mats_div.select(SP_MAT_CONTENT_SELECTOR):
            if "gear" in heading:
                ref_id = _match_re(_EQUIP_ID_RE, td.select_one(EQUIP_REF_TAG[0]), EQUIP_REF_TAG[1])
            elif "items" in heading:
                ref_id = _match_re(_ITEM_ID_RE, td.select_one(ITEM_REF_TAG[0]), ITEM_REF_TAG[1])

            mat_info = _text(td).split(" × ")
            if ref_id and mat_info:
                entry[ref_id] = (mat_info[0], mat_info[1])
        if entry:
            mats.append({heading: entry})
    return mats or None

def fetch_sp_evo(soup: BeautifulSoup):
    return _fetch_evo(soup, "dl.detail__reincarnation"), _fetch_sp_materials(soup)
