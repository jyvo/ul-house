with recursive edges as (select kind, from_uid, to_uid from {{ ref('mart_evolution_edge') }}),
walk(kind, start_uid, uid, path) as (
  select kind, from_uid, to_uid, [from_uid, to_uid] from edges
  union all
  select w.kind, w.start_uid, e.to_uid, list_append(w.path, e.to_uid)
  from walk w join edges e on e.kind = w.kind and e.from_uid = w.uid
  where w.uid <> w.start_uid and len(w.path) < 1000
    and (e.to_uid = w.start_uid or not list_contains(w.path, e.to_uid))
)
select distinct kind, start_uid
from walk
where uid = start_uid
