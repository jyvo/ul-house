select s.entity_type, s.entity_key, s.row_hash, s.revision
from {{ ref('int_changes') }} s
where s.operation <> 'ALIAS'
{% if is_incremental() %}
  and not exists (select 1 from {{ this }} t
                  where t.entity_type = s.entity_type and t.entity_key = s.entity_key and t.revision = s.revision)
{% endif %}
