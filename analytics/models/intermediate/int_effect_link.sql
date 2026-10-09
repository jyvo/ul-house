-- ordered effects per owner
select distinct * from (
  select 'proc' as owner_kind, cast(p.proc_id as varchar) as owner_id, l.ordinal, l.effect_id
  from {{ ref('int_effect_line') }} l
  join {{ ref('int_proc') }} p on p.source_uid = l.source_uid
  where l.owner_kind = 'proc'
  union all
  select 'weapon_ability', a.uid, l.ordinal, l.effect_id
  from {{ ref('int_effect_line') }} l
  join {{ ref('int_weapon_ability') }} a on a.source_uid = l.source_uid
  where l.owner_kind = 'weapon_ability'
  union all
  select 'monster_skill', cast(s.skill_id as varchar), l.ordinal, l.effect_id
  from {{ ref('int_effect_line') }} l
  join {{ ref('int_monster_skill') }} s on s.uid = l.source_uid and s.ordinal = l.owner_ordinal
  where l.owner_kind = 'monster_skill'
  union all
  select 'passive_skill', cast(ps.passive_id as varchar), l.ordinal, l.effect_id
  from {{ ref('int_effect_line') }} l
  join {{ ref('int_passive_skill') }} ps on ps.source_uid = l.source_uid
  where l.owner_kind = 'passive_skill'
)
