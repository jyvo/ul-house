-- mart.duckdb main holds shipped tables of database/schema.sql (game + ledger)
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
-- depends_on: {{ ref('mart_equipment') }}
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
-- depends_on: {{ ref('mart_uid_retired') }}
-- depends_on: {{ ref('mart_uid_alias') }}
with expected as (select * from (values ('element'), ('element_relation'), ('item'), ('icon'), ('skill_effect'), ('proc_family'), ('proc'), ('proc_condition'), ('proc_scaling'), ('weapon_ability'), ('passive_skill'), ('equipment'), ('weapon'), ('defensive_gear'), ('monster'), ('monster_skill'), ('potential_level'), ('effect_link'), ('stat'), ('evolution_edge'), ('evolution_chain'), ('evolution_material'), ('uid_retired'), ('uid_alias')) v(table_name)),
actual as (
  select table_name from information_schema.tables
  where table_catalog = current_database() and table_schema = 'main'
)
select coalesce(e.table_name, a.table_name) as table_name,
       case when a.table_name is null then 'missing' else 'unexpected' end as problem
from expected e
full outer join actual a on a.table_name = e.table_name
where e.table_name is null or a.table_name is null
