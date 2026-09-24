import re


BASE_URL = "https://jam-capture-unisonleague-ww.ateamid.com"

# webpage query params
PAGE_GROUPS = {"1": "weapon", "23": "armor", "4": "monster"}
RARITY_SUFFIX = {"5": "UR", "4": "SSR"}

WEAPON_TYPES = ("sword", "axe", "lance", "scythe", "bow", "gun", "staff", "book", "relic", "dual blade")
MONSTER_TYPE = "monster"
STAT_LABELS = {"atk", "matk", "def", "mdef"}

# ref id url regex
EQUIP_ID_RE = re.compile(r"equip_detail/(\d+)\.html")
ITEM_ID_RE = re.compile(r"itemicon/item_(\d+)\.png")
ABILITY_ID_RE = re.compile(r"ability_detail/(\d+)\.html")

# name tokens
# progression gear suffixes
PROGRESSION_TOKENS = ("xeno", "sopho")
EXCLUDED_NAME_TOKENS = ("awakening ninoyu",)

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
