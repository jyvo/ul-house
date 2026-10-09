with eq as (select * from {{ ref('mart_equipment') }} where state = 'live'),
stat_gaps as (
  select uid, 'stat ' || label || ' has no max tier' as gap
  from {{ ref('mart_stat') }}
  group by uid, label
  having bool_or(tier = 'initial') and not bool_or(tier like 'max%')
),
gaps as (
  select uid, gap from stat_gaps
  union all
  select uid, 'max_level = 0' from eq where max_level = 0
  union all
  select uid, 'weapon without proc' from {{ ref('mart_weapon') }} where proc_id is null
  union all
  select uid, 'defensive gear without proc' from {{ ref('mart_defensive_gear') }} where proc_id is null
  union all
  select m.uid, 'monster without skills' from {{ ref('mart_monster') }} m
  where not exists (select 1 from {{ ref('mart_monster_skill') }} s where s.uid = m.uid)
)
select {{ gate_row("'equipment'", 'eq.uid', 'eq.entry_kind', 'f.is_new_item', '1', 'null',
                   "string_agg(g.gap, '; ' order by g.gap)") }}
from eq
join gaps g on g.uid = eq.uid
join {{ ref('int_first_seen') }} f on f.uid = eq.uid
group by eq.uid, eq.entry_kind, f.is_new_item
