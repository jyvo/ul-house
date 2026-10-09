select p.entity_key as uid
from {{ source('prev', 'entity_current') }} p
where p.entity_type = 'equipment' and p.last_operation <> 'DELETE'
  and p.entity_key not in (select uid from {{ ref('mart_equipment') }})
  and p.entity_key not in (select source_id from {{ ref('stg_parse_error') }})
