-- listing vs frontier vs shipped
select coalesce(l.item_id, f.uid, e.uid) as uid,
       l.item_id is not null as listed, f.uid is not null as in_frontier, f.in_scope,
       f.frontier_state, e.uid is not null as shipped, e.state
from {{ ref('stg_listing') }} l
full outer join {{ ref('int_scope_member') }} f on f.uid = l.item_id
full outer join {{ ref('mart_equipment') }} e on e.uid = coalesce(l.item_id, f.uid)
