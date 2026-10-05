import re
from importlib.metadata import PackageNotFoundError, version


BASE_URL = "https://jam-capture-unisonleague-ww.ateamid.com"

try:
    CRAWLER_VERSION = version("ul-house")
except PackageNotFoundError:
    CRAWLER_VERSION = "0"
PROJECT_URL = "https://github.com/jyvo/ul-house"
CONTACT_URL = f"{PROJECT_URL}/issues"
USER_AGENT = f"ul-house/{CRAWLER_VERSION} (+{CONTACT_URL})"

# list pages -- the crawl frontier, never parsed into models
LIST_PAGE_GROUPS = {
    "1": "weapon",
    "23": "armor",
    "4": "monster",
}

RARITY_LIST_PAGE = {"5": "UR", "4": "SSR"}

LIST_SOURCES = tuple(
    (f"/en/equip_list/{group}_{suffix}.html", LIST_PAGE_GROUPS[group], rarity)
    for group in LIST_PAGE_GROUPS
    for suffix, rarity in RARITY_LIST_PAGE.items()
)

NEW_RELEASE_PATH = "/en/new_release_list.html"

def detail_path(equip_id: str) -> str:
    """page-store key: urls are stored relative to BASE_URL"""
    return f"/en/equip_detail/{equip_id}.html"


def detail_url(equip_id: str) -> str:
    return f"{BASE_URL}{detail_path(equip_id)}"

# list row
LIST_ROW_SELECTOR = "td.filter"
LIST_ROW_NAME_SELECTOR = "p.list_item_name"
LIST_ROW_INPUT = "input[name=unisonleague_{field}]"

# evo links (crawl reads these without going through parse)
EVO_LINK_LABEL_SELECTOR = "dt.detail__evo--last"
EVO_LINK_NAME_SELECTOR = "p.evo_name"
EVO_LINK_RE = re.compile(r"^(?P<side>before|after)\s+(?P<kind>reforging|awakening|enlightening)$")
EVO_KIND = {"reforging": "reforge", "awakening": "awakening", "enlightening": "enlightening"}

WEAPON_TYPES = ("sword", "axe", "lance", "scythe", "bow", "gun", "staff", "book", "relic", "dual blade")
MONSTER_TYPE = "monster"
STAT_LABELS = {"atk", "matk", "def", "mdef"}

# small icons
EQUIP_ICON_PATH = "/images/equipicon/{uid}.png"

# ref id url regex
EQUIP_ID_RE = re.compile(r"equip_detail/(\d+)\.html")
ITEM_ID_RE = re.compile(r"itemicon/item_(\d+)\.png")
ABILITY_ID_RE = re.compile(r"ability_detail/(\d+)\.html")


# soup selectors 
HEADING_SELECTOR = "p.title_bar--text"
NAME_SELECTOR = "p.name__text"
BASIC_DATA_SELECTOR = "div.detail__data dl.detail__data--txt"
STATS_NAME_SELECTOR = "dl.detail__status--name dd"
STATS_SELECTOR = "div.detail__status dl"
SKILLS_SELECTOR = "div.detail__skill", "dl.detail__evo dt"
WEAPON_ABILITY_SELECTOR = "div.detail__ability dl.detail__ability--txt dt"
REFORGE_SELECTOR = "dl.detail__evo"
REFORGE_MAT_SELECTOR = "dl.detail__evo dd.detail__material_evo--last"
SP_EVO_SELECTOR = "dl.detail__reincarnation"
SP_MAT_TITLE_SELECTOR = "div.sp_evo_title"
SP_MAT_CONTENT_NAME = "sp_evo_contents"
SP_MAT_CONTENT_SELECTOR = "table.data tbody tr td.special_evolution_material_block"
# void selector in restriction note
RESTRICTION_NOTE_SELECTOR = "div"

# ref selectors
EQUIP_REF_TAG = "a", "href"
ITEM_REF_TAG = "img", "data-src"

# parser
STAT_TIER_RE = re.compile(r"[^a-z0-9]")
ABSENT_VAL = "-"
NUM_MAT_SEP = " × "
# - skill effect
EFFECT_SPLIT_RE = re.compile(r"(?:^|\s)-(?=\S)")
TARGET_RE = re.compile(r"^target:\s*(?P<target>[^.]+?)\s*\.\s*(?P<description>.+)$")
# | proc
SECTION_RE = re.compile(r"^\[(?P<name>[^\]]+)\]$")
EFFECTS, ACTIVATION, ACTIVATION_RATE = "effects", "activation", "activation rate"
SCALES = "scales"
# | monster
AMPLIFIER_RE = re.compile(r"\s*(ability power boosted by \d+ for each increase in skill level\.)\s*$")
POTENTIAL_RE = re.compile(r"lv\s*(\d+)")
SKILL_HEADING_RE = re.compile(r"^skill(\s*#\d+)?$")
PASSIVE_HEADING = "passive skill"
