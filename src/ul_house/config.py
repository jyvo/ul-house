import re

BASE_URL = "https://jam-capture-unisonleague-ww.ateamid.com"

WEAPON_TYPES = ("Sword", "Axe", "Lance", "Scythe", "Bow", "Gun", "Staff", "Book", "Relic", "Dual Blade")
STAT_LABELS = {"ATK", "MATK", "DEF", "MDEF"}

# ref id url regex
_EQUIP_ID_RE = re.compile(r"equip_detail/(\d+)\.html")
_ITEM_ID_RE = re.compile(r"itemicon/item_(\d+)\.png")
_ABILITY_ID_RE = re.compile(r"ability_detail/(\d+)\.html")

# webpage query params
PAGE_GROUPS = {"1": "weapon", "23": "armor", "4": "monster"}
RARITY_SUFFIX = {"5": "UR", "4": "SSR"}

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
# STATS_VAL_SELECTOR = "dl.detail__status--min, dl.detail__status--max"
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

# heading labels


SKILL_HEADING = "Skill" # Skill / Skill #1 / Skill #2
PASSIVE_HEADING = "Passive Skill"
HIDDEN_POTENTIAL_HEADING = "Hidden Potential" # shared with restrictions
REFORGE_HEADING = "Reforge Info"
AWAKENING_HEADING = "Awakening Info"
ENLIGHTENING_HEADING = "Enlightening Info"
