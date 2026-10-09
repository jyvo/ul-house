select s.revision, s.entity_type, s.entity_key, s.operation, s.row_hash, s.changed_at
from {{ ref('int_changes') }} s
{% if is_incremental() %}
where not exists (select 1 from {{ this }} t
                  where t.revision = s.revision and t.entity_type = s.entity_type and t.entity_key = s.entity_key)
{% endif %}
