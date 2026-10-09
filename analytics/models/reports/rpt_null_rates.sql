select metric, value
from (
  select 'equipment.icon_sha' as metric, avg(case when icon_sha is null then 1.0 else 0.0 end) as value from {{ ref('mart_equipment') }}
  union all select 'proc.element_id', avg(case when element_id is null then 1.0 else 0.0 end) from {{ ref('mart_proc') }}
  union all select 'proc.size', avg(case when size is null then 1.0 else 0.0 end) from {{ ref('mart_proc') }}
  union all select 'weapon.proc_id', avg(case when proc_id is null then 1.0 else 0.0 end) from {{ ref('mart_weapon') }}
  union all select 'defensive_gear.proc_id', avg(case when proc_id is null then 1.0 else 0.0 end) from {{ ref('mart_defensive_gear') }}
  union all select 'monster.passive_id', avg(case when passive_id is null then 1.0 else 0.0 end) from {{ ref('mart_monster') }}
  union all select 'skill_effect.target', avg(case when target is null then 1.0 else 0.0 end) from {{ ref('mart_skill_effect') }}
)
