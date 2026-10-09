select entity_type, entity_key, count(*) as operations
from {{ ref('int_changes') }}
group by entity_type, entity_key
having count(*) > 1
