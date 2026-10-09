with claims as (select * from {{ ref('int_evolution_claim') }}),
multi as (
  select kind, to_uid, string_agg(distinct from_uid, ', ' order by from_uid) as predecessors
  from claims group by kind, to_uid
  having count(distinct from_uid) > 1
)
select {{ gate_row("'equipment'", 'm.to_uid', "coalesce(f.entry_kind, 'catalog')", 'false', '1', 'null',
                   "m.kind || ': ' || m.predecessors") }}
from multi m
left join {{ ref('int_scope_member') }} f on f.uid = m.to_uid
