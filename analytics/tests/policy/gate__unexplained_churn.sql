select {{ gate_row("'equipment'", 'c.entity_key', 'e.entry_kind', 'f.is_new_item') }}
from {{ ref('int_changes') }} c
cross join {{ ref('int_run') }} r
join {{ ref('mart_equipment') }} e on e.uid = c.entity_key
join {{ ref('int_first_seen') }} f on f.uid = c.entity_key
join {{ ref('int_equipment_status') }} s on s.uid = c.entity_key
where c.entity_type = 'equipment' and c.operation = 'UPDATE'
  and not s.revived
  and r.prev_parser_version is not null
  and r.parser_version = r.prev_parser_version
  and coalesce(r.prev_transform_version, r.transform_version) = r.transform_version
