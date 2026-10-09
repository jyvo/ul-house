-- old = retired within alias_window_days, new = live first seen within window (same info)
with r as (select * from {{ ref('int_run') }}),
window_start as (
  select {{ iso_ts('started_at') }} - to_days(cast({{ var('alias_window_days') }} as integer)) as since from r
),
revision_time as (
  select revision, min(started_at) as started_at from {{ source('prev', 'transform_run') }} group by revision
),
retirements as (
  select uid, since_revision from {{ source('prev', 'uid_retired') }} where reason = 'gone'
  union all
  select uid, since_revision from {{ ref('int_retired_new') }} where reason = 'gone'
),
attrs as (
  select s.uid, lower(s.name) as name, st.model_class, s.rarity, s.element_id, s.cost, s.state,
         st.retire_reason, f.first_seen
  from {{ ref('int_ship_equipment') }} s
  join {{ ref('int_equipment_status') }} st on st.uid = s.uid
  join {{ ref('int_first_seen') }} f on f.uid = s.uid
),
old as (
  select a.* from attrs a
  join retirements t on t.uid = a.uid
  cross join r
  left join revision_time rt on rt.revision = t.since_revision
  where a.state = 'retired' and a.retire_reason = 'gone'
    and {{ iso_ts('coalesce(rt.started_at, r.started_at)') }} >= (select since from window_start)
),
new as (
  select a.* from attrs a
  where a.state = 'live' and {{ iso_ts('a.first_seen') }} >= (select since from window_start)
),
pairs as (
  select o.uid as old_uid, n.uid as new_uid, o.name, o.model_class, o.rarity, o.element_id, o.cost
  from old o
  join new n on n.name = o.name and n.model_class = o.model_class and n.rarity = o.rarity
            and n.element_id = o.element_id and n.cost = o.cost and n.uid <> o.uid
),
cells as (
  select p.old_uid, p.new_uid, s.label, s.tier, s.value, 'o' as side
  from pairs p join {{ ref('mart_stat') }} s on s.uid = p.old_uid
  union all
  select p.old_uid, p.new_uid, s.label, s.tier, s.value, 'n'
  from pairs p join {{ ref('mart_stat') }} s on s.uid = p.new_uid
),
cell_sides as (
  select old_uid, new_uid, label, tier, value, count(distinct side) as sides
  from cells group by old_uid, new_uid, label, tier, value
),
scores as (
  select p.old_uid, p.new_uid, p.name, p.model_class, p.rarity, p.element_id, p.cost,
         coalesce(sum(case when c.sides = 2 then 1 else 0 end), 0) as stats_equal,
         count(c.label)                                             as stats_total
  from pairs p
  left join cell_sides c on c.old_uid = p.old_uid and c.new_uid = p.new_uid
  group by all
)
select s.old_uid, s.new_uid, r.revision as first_seen_revision,
       cast(case when s.stats_total = 0 then 1.0 else s.stats_equal / s.stats_total end as double) as score,
       cast(json_object('name', s.name, 'class', s.model_class, 'rarity', s.rarity, 'element', s.element_id,
                        'cost', s.cost, 'stats_equal', s.stats_equal, 'stats_total', s.stats_total) as varchar) as evidence
from scores s
cross join r
where (case when s.stats_total = 0 then 1.0 else s.stats_equal / s.stats_total end) >= {{ var('alias_min_score') }}
  and not exists (select 1 from {{ source('prev', 'alias_candidate') }} c where c.old_uid = s.old_uid and c.new_uid = s.new_uid)
  and s.old_uid not in (select old_uid from {{ source('prev', 'uid_alias') }})
  and not exists (select 1 from {{ source('prev', 'alias_rejected') }} j where j.old_uid = s.old_uid and j.new_uid = s.new_uid)
  and not exists (select 1 from {{ ref('rejected_aliases') }} j where trim(j.old_uid) = s.old_uid and trim(j.new_uid) = s.new_uid)
  and s.old_uid not in (select trim(old_uid) from {{ ref('confirmed_aliases') }} where old_uid is not null)
