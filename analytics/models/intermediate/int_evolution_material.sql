-- attach materials to outgoing edge
with successors as (
  select kind, from_uid, min(to_uid) as to_uid, count(*) as n
  from {{ ref('int_evolution_edge') }}
  group by kind, from_uid
)
select m.kind,
       case when s.n = 1 then m.source_uid end as from_uid,
       case when s.n = 1 then s.to_uid end as to_uid,
       m.ordinal, m.material_kind, m.ref_uid,
       coalesce({{ clean_text('m.ref_name', false) }}, '') as ref_name,
       m.quantity, m.source_uid,
       coalesce(s.n = 1, false) as attributed
from {{ ref('stg_evolution_material') }} m
left join successors s on s.kind = m.kind and s.from_uid = m.source_uid
