with materials as (
  select m.*, ev.before_uid, ev.after_uid
  from {{ ref('stg_evolution_material') }} m
  left join {{ ref('stg_evolution') }} ev on ev.source_uid = m.source_uid and ev.kind = m.kind
),
placed as (
  select kind,
         case when after_uid is not null then source_uid when before_uid is not null then before_uid end as from_uid,
         case when after_uid is not null then after_uid when before_uid is not null then source_uid end as to_uid,
         ordinal, material_kind, ref_uid,
         coalesce({{ clean_text('ref_name', false) }}, '') as ref_name,
         quantity, source_uid,
         case when after_uid is not null then 0 else 1 end as preference
  from materials
)
select kind, from_uid, to_uid, ordinal, material_kind, ref_uid, ref_name, quantity, source_uid, true as attributed
from placed
where from_uid is not null
qualify dense_rank() over (partition by kind, from_uid, to_uid order by preference, source_uid) = 1
union all
select kind, from_uid, to_uid, ordinal, material_kind, ref_uid, ref_name, quantity, source_uid, false
from placed
where from_uid is null
