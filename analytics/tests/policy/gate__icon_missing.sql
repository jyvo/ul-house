select {{ gate_row("'equipment'", 'e.uid', 'e.entry_kind', 'f.is_new_item') }}
from {{ ref('mart_equipment') }} e
join {{ ref('int_first_seen') }} f on f.uid = e.uid
where e.state = 'live' and e.icon_sha is null
