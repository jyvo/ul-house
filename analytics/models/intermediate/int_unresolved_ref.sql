-- unshipped evo endpoints
with shipped as (select uid from {{ ref('int_ship_equipment') }})
select kind, from_uid as uid, 'from' as role, to_uid as other_uid, from_name as name
from {{ ref('int_evolution_edge') }}
where from_uid not in (select uid from shipped)
union all
select kind, to_uid, 'to', from_uid, to_name
from {{ ref('int_evolution_edge') }}
where to_uid not in (select uid from shipped)
