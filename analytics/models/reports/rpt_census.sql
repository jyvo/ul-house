-- counts per scope × state × rarity × gear type (§5.8 report; never shipped)
select entry_kind, state, rarity, gear_type, count(*) as items
from {{ ref('mart_equipment') }}
group by all
order by all
