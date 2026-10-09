with cur as (
  select entity_type, entity_key from {{ ref('int_aggregate_hash_game') }}
  union all
  select entity_type, entity_key from {{ ref('int_aggregate_hash_ledger') }}
),
prev as (select * from {{ source('prev', 'entity_current') }} where last_operation <> 'DELETE'),
flags as ({{ registry_flags() }})
select p.entity_type, p.entity_key
from prev p
join flags f on f.entity_type = p.entity_type and f.deletable
where not exists (select 1 from cur c where c.entity_type = p.entity_type and c.entity_key = p.entity_key)
  and exists (select 1 from {{ ref('stg_parse_error') }} pe
              join prev q on q.entity_type = 'equipment' and q.entity_key = pe.source_id)
