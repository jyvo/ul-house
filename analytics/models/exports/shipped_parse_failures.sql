select pe.source_id as uid, pe.source_hash, cast(pe.parser_version as bigint) as parser_version, pe.error
from {{ ref('stg_parse_error') }} pe
where exists (select 1 from {{ source('prev', 'entity_current') }} p
              where p.entity_type = 'equipment' and p.entity_key = pe.source_id
                and p.last_operation <> 'DELETE')
