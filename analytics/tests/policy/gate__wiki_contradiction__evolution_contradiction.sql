with claims as (select * from {{ ref('int_evolution_claim') }})
select distinct {{ gate_row("'equipment'", 'a.from_uid', "coalesce(f.entry_kind, 'catalog')", 'false', '1', 'null',
                   "a.kind || ': after ' || a.to_uid || ', before-claimed ' || b.to_uid") }}
from claims a
join claims b on b.kind = a.kind and b.from_uid = a.from_uid and b.side = 'before' and b.to_uid <> a.to_uid
left join {{ ref('int_scope_member') }} f on f.uid = a.from_uid
where a.side = 'after'
