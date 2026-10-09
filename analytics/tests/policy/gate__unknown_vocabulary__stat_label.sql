select distinct {{ gate_row("'equipment'", 'e.uid', 'q.entry_kind', 'f.is_new_item', '1', 'null', "'label ' || e.label") }}
from {{ ref('mart_stat') }} e
join {{ ref('mart_equipment') }} q on q.uid = e.uid
join {{ ref('int_first_seen') }} f on f.uid = e.uid
where e.label not in (select value from {{ ref('vocabulary') }} where domain = 'stat_label')
