select element_id, scale_kind, gear_kind, count(*) as lines
from {{ ref('mart_proc_scaling') }}
group by all
order by all
