select distinct {{ gate_row("'equipment'", 'e.uid', 'q.entry_kind', 'f.is_new_item', '1', 'null', "'gear_type ' || e.gear_type") }}
from {{ ref('mart_equipment') }} e
join {{ ref('mart_equipment') }} q on q.uid = e.uid
join {{ ref('int_first_seen') }} f on f.uid = e.uid
where e.gear_type not in (select value from {{ ref('vocabulary') }} where domain = 'gear_type')
