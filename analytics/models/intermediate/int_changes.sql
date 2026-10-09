-- change log of revision N + hashes / state entity_current needs -> one operation per (entity_type, entity_key)
with cur as (
  select entity_type, entity_key, row_hash, content_hash, state, retired_revision
  from {{ ref('int_aggregate_hash_game') }}
  union all
  select entity_type, entity_key, row_hash, content_hash, state, retired_revision
  from {{ ref('int_aggregate_hash_ledger') }}
),
prev as (
  select * from {{ source('prev', 'entity_current') }} where last_operation <> 'DELETE'
),
flags as ({{ registry_flags() }}),
-- defer when uid fail to parse
deletes_deferred as (
  select exists (
    select 1 from {{ ref('stg_parse_error') }} pe
    join prev p on p.entity_type = 'equipment' and p.entity_key = pe.source_id
  ) as deferred
),
ops as (
  select c.entity_type, c.entity_key,
    case when p.entity_key is null then 'INSERT'
         when p.row_hash = c.row_hash then null
         when c.entity_type = 'equipment' and p.content_hash = c.content_hash
              and p.state = 'live' and c.state = 'retired' then 'RETIRE'
         else 'UPDATE' end as operation,
    c.row_hash, c.content_hash, c.state, c.retired_revision
  from cur c
  left join prev p on p.entity_type = c.entity_type and p.entity_key = c.entity_key
  union all
  select p.entity_type, p.entity_key, 'DELETE', null, null, null, null
  from prev p
  join flags f on f.entity_type = p.entity_type and f.deletable
  cross join deletes_deferred d
  where not d.deferred
    and not exists (select 1 from cur c where c.entity_type = p.entity_type and c.entity_key = p.entity_key)
  union all
  select 'equipment', a.old_uid, 'ALIAS', c.row_hash, c.content_hash, c.state, c.retired_revision
  from {{ ref('int_alias_new') }} a
  join cur c on c.entity_type = 'equipment' and c.entity_key = a.old_uid
)
select r.revision, o.entity_type, o.entity_key, o.operation, o.row_hash,
       r.started_at as changed_at, o.content_hash, o.state, o.retired_revision
from ops o
cross join {{ ref('int_run') }} r
where o.operation is not null
