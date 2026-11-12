-- SR only for awakening predecessors
with progression as (
  select e.uid
  from {{ ref('mart_equipment') }} e
  join {{ ref('int_scope_member') }} f on f.uid = e.uid
  join {{ ref('mart_evolution_edge') }} g
    on g.kind = 'awakening' and g.from_uid = e.uid and g.to_uid = f.parent_id
  join {{ ref('mart_equipment') }} k on k.uid = g.to_uid
  where f.source = 'evolution'
    and f.entry_kind = 'reference'
    and f.keep_reason = 'evolution:awakening'
    and k.state = 'live'
)
select e.uid, e.name, e.rarity, e.entry_kind, e.keep_reason
from {{ ref('mart_equipment') }} e
where e.rarity not in ('UR', 'SSR')
  and not (e.rarity = 'SR' and e.uid in (select uid from progression))
