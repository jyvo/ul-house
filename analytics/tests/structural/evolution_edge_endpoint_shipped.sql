select 'evolution_edge' as table_name, kind, from_uid, to_uid
from {{ ref('mart_evolution_edge') }}
where from_uid not in (select uid from {{ ref('mart_equipment') }})
  and to_uid not in (select uid from {{ ref('mart_equipment') }})
union all
select distinct 'evolution_material', kind, from_uid, to_uid
from {{ ref('mart_evolution_material') }}
where from_uid not in (select uid from {{ ref('mart_equipment') }})
  and to_uid not in (select uid from {{ ref('mart_equipment') }})
