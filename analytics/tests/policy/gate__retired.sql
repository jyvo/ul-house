select {{ gate_row("'equipment'", 's.uid', 'e.entry_kind', 'f.is_new_item', '1', 'null', 's.retire_reason') }}
from {{ ref('int_equipment_status') }} s
join {{ ref('mart_equipment') }} e on e.uid = s.uid
join {{ ref('int_first_seen') }} f on f.uid = s.uid
where s.newly_retired
