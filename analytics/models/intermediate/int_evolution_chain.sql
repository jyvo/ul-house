with recursive edges as (
  select kind, from_uid, to_uid from {{ ref('int_evolution_edge') }}
),
nodes as (
  select kind, from_uid as uid from edges
  union
  select kind, to_uid from edges
),
roots as (
  select n.kind, n.uid from nodes n
  where not exists (select 1 from edges e where e.kind = n.kind and e.to_uid = n.uid)
),
walk(kind, chain_id, uid, position, path) as (
  select kind, uid, uid, 0, [uid] from roots
  union all
  select w.kind, w.chain_id, e.to_uid, w.position + 1, list_append(w.path, e.to_uid)
  from walk w
  join edges e on e.kind = w.kind and e.from_uid = w.uid
  where not list_contains(w.path, e.to_uid) and w.position < 1000
)
select w.kind, w.chain_id, w.uid, cast(w.position as bigint) as position
from walk w
join {{ ref('int_ship_equipment') }} s on s.uid = w.uid
qualify row_number() over (partition by w.kind, w.uid order by w.position, w.chain_id) = 1
