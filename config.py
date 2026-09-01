BASE_URL = "https://jam-capture-unisonleague-ww.ateamid.com"

# webpage query params
PAGE_GROUPS = {"1": "weapon", "23": "armor", "4": "monster"}
RARITY_SUFFIX = {"5": "UR", "4": "SSR"}

# name tokens
# progression gear suffixes
PROGRESSION_TOKENS = ("xeno", "sopho")
EXCLUDED_NAME_TOKENS = ("awakening ninoyu",)

# parser 
NAME_SELECTOR = "p.name__text"
BASIC_INFO_SELECTOR = "div.detail__data dl.detail__data--txt"
STATS_NAME_SELECTOR = "dl.detail__status--name dd"
STATS_SELECTOR = "div.detail__status"
STATS_VAL_SELECTOR = "dl.detail__status--min, dl.detail__status--max"
SKILLS_SELECTOR = "div.detail__skill"
WEAPON_ABILITY_SELECTOR = "div.detail__ability dl.detail__ability--txt"

# awakening / enlightening selector
SP_EVO_SELECTOR = "dl.detail__reincarnation"
SP_EVO_MAT_TITLE_SELECTOR = "div.sp_evo_title"
SP_EVO_MAT_CONTENT_CLASSNAME = "sp_evo_contents"
SP_EVO_MAT_BLOCK_SELECTOR = "td.special_evolution_material_block"
