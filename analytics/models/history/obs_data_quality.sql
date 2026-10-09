-- data-quality metrics (prev vals)
with metrics as (
  select 'count:element' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_element') }}
  union all
  select 'count:element_relation' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_element_relation') }}
  union all
  select 'count:item' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_item') }}
  union all
  select 'count:icon' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_icon') }}
  union all
  select 'count:skill_effect' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_skill_effect') }}
  union all
  select 'count:proc_family' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_proc_family') }}
  union all
  select 'count:proc' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_proc') }}
  union all
  select 'count:proc_condition' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_proc_condition') }}
  union all
  select 'count:proc_scaling' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_proc_scaling') }}
  union all
  select 'count:weapon_ability' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_weapon_ability') }}
  union all
  select 'count:passive_skill' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_passive_skill') }}
  union all
  select 'count:equipment' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_equipment') }}
  union all
  select 'count:weapon' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_weapon') }}
  union all
  select 'count:defensive_gear' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_defensive_gear') }}
  union all
  select 'count:monster' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_monster') }}
  union all
  select 'count:monster_skill' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_monster_skill') }}
  union all
  select 'count:potential_level' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_potential_level') }}
  union all
  select 'count:effect_link' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_effect_link') }}
  union all
  select 'count:stat' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_stat') }}
  union all
  select 'count:evolution_edge' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_evolution_edge') }}
  union all
  select 'count:evolution_chain' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_evolution_chain') }}
  union all
  select 'count:evolution_material' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_evolution_material') }}
  union all
  select 'count:uid_retired' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_uid_retired') }}
  union all
  select 'count:uid_alias' as metric, 'all' as scope, cast(count(*) as double) as value from {{ ref('mart_uid_alias') }}
  union all
  select 'population', scope, cast(value as double) from {{ ref('int_population') }}
  union all
  select 'null_rate:equipment.icon_sha', 'all', cast(avg(case when "icon_sha" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_equipment') }}
  union all
  select 'null_rate:equipment.retired_revision', 'all', cast(avg(case when "retired_revision" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_equipment') }}
  union all
  select 'null_rate:proc.element_id', 'all', cast(avg(case when "element_id" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_proc') }}
  union all
  select 'null_rate:proc.size', 'all', cast(avg(case when "size" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_proc') }}
  union all
  select 'null_rate:proc.activation_rate', 'all', cast(avg(case when "activation_rate" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_proc') }}
  union all
  select 'null_rate:proc.element_position', 'all', cast(avg(case when "element_position" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_proc') }}
  union all
  select 'null_rate:proc_scaling.element_id', 'all', cast(avg(case when "element_id" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_proc_scaling') }}
  union all
  select 'null_rate:proc_scaling.gear_kind', 'all', cast(avg(case when "gear_kind" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_proc_scaling') }}
  union all
  select 'null_rate:proc_scaling.cap', 'all', cast(avg(case when "cap" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_proc_scaling') }}
  union all
  select 'null_rate:proc_scaling.pieces_needed', 'all', cast(avg(case when "pieces_needed" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_proc_scaling') }}
  union all
  select 'null_rate:skill_effect.target', 'all', cast(avg(case when "target" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_skill_effect') }}
  union all
  select 'null_rate:weapon.proc_id', 'all', cast(avg(case when "proc_id" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_weapon') }}
  union all
  select 'null_rate:weapon.ability_uid', 'all', cast(avg(case when "ability_uid" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_weapon') }}
  union all
  select 'null_rate:defensive_gear.proc_id', 'all', cast(avg(case when "proc_id" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_defensive_gear') }}
  union all
  select 'null_rate:monster.passive_id', 'all', cast(avg(case when "passive_id" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_monster') }}
  union all
  select 'null_rate:monster.restrictions', 'all', cast(avg(case when "restrictions" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_monster') }}
  union all
  select 'null_rate:evolution_material.ref_uid', 'all', cast(avg(case when "ref_uid" is null then 1.0 else 0.0 end) as double) from {{ ref('mart_evolution_material') }}
  union all
  select 'orphans', 'all', cast(count(*) as double) from {{ ref('int_unresolved_ref') }}
  union all
  select 'icons_missing', 'all', cast(count(*) as double) from {{ ref('int_ship_equipment') }} where state = 'live' and icon_sha is null
  union all
  select 'retirements', 'all', cast(count(*) as double) from {{ ref('int_equipment_status') }} where newly_retired
  union all
  select 'alias_candidates', 'all', cast(count(*) as double) from {{ ref('int_alias_candidate_new') }}
  union all
  select 'parse_failures', 'all', cast(count(*) as double) from {{ ref('stg_parse_error') }}
),
s as (
  select r.run_id, m.metric, m.scope, m.value, p.value as prev_value,
         case when p.value is null or p.value = 0 then null
              else 100.0 * (m.value - p.value) / p.value end as delta_pct,
         r.started_at as observed_at
  from metrics m
  cross join {{ ref('int_run') }} r
  left join {{ source('prev', 'obs_data_quality') }} p
    on p.run_id = r.prev_release_run_id and p.metric = m.metric and p.scope = m.scope
)
select run_id, metric, scope, cast(value as double) as value, cast(prev_value as double) as prev_value,
       cast(delta_pct as double) as delta_pct, observed_at
from s
{% if is_incremental() %}
where not exists (select 1 from {{ this }} t where t.run_id = s.run_id and t.metric = s.metric and t.scope = s.scope)
{% endif %}
