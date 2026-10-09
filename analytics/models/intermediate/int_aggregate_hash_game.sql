-- (entity_type, entity_key, row_hash, content_hash, state, retired_revision)
-- depends_on: {{ ref('mart_element') }}
-- depends_on: {{ ref('mart_element_relation') }}
-- depends_on: {{ ref('mart_item') }}
-- depends_on: {{ ref('mart_icon') }}
-- depends_on: {{ ref('mart_skill_effect') }}
-- depends_on: {{ ref('mart_proc_family') }}
-- depends_on: {{ ref('mart_proc') }}
-- depends_on: {{ ref('mart_proc_condition') }}
-- depends_on: {{ ref('mart_proc_scaling') }}
-- depends_on: {{ ref('mart_weapon_ability') }}
-- depends_on: {{ ref('mart_passive_skill') }}
-- depends_on: {{ ref('int_ship_equipment') }}
-- depends_on: {{ ref('mart_weapon') }}
-- depends_on: {{ ref('mart_defensive_gear') }}
-- depends_on: {{ ref('mart_monster') }}
-- depends_on: {{ ref('mart_monster_skill') }}
-- depends_on: {{ ref('mart_potential_level') }}
-- depends_on: {{ ref('mart_effect_link') }}
-- depends_on: {{ ref('mart_stat') }}
-- depends_on: {{ ref('mart_evolution_edge') }}
-- depends_on: {{ ref('mart_evolution_chain') }}
-- depends_on: {{ ref('mart_evolution_material') }}
{{ aggregate_hashes(
  ['equipment', 'proc', 'proc_family', 'skill_effect', 'passive_skill', 'weapon_ability', 'item', 'icon', 'element'],
  relations={
    'element': ref('mart_element'),
    'element_relation': ref('mart_element_relation'),
    'item': ref('mart_item'),
    'icon': ref('mart_icon'),
    'skill_effect': ref('mart_skill_effect'),
    'proc_family': ref('mart_proc_family'),
    'proc': ref('mart_proc'),
    'proc_condition': ref('mart_proc_condition'),
    'proc_scaling': ref('mart_proc_scaling'),
    'weapon_ability': ref('mart_weapon_ability'),
    'passive_skill': ref('mart_passive_skill'),
    'equipment': ref('int_ship_equipment'),
    'weapon': ref('mart_weapon'),
    'defensive_gear': ref('mart_defensive_gear'),
    'monster': ref('mart_monster'),
    'monster_skill': ref('mart_monster_skill'),
    'potential_level': ref('mart_potential_level'),
    'effect_link': ref('mart_effect_link'),
    'stat': ref('mart_stat'),
    'evolution_edge': ref('mart_evolution_edge'),
    'evolution_chain': ref('mart_evolution_chain'),
    'evolution_material': ref('mart_evolution_material'),
  },
  exclude={'equipment': ['state', 'retired_revision']}
) }}
