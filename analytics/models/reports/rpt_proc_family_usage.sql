select f.name as family, p.element_id, p.size, count(*) as procs
from {{ ref('mart_proc') }} p
join {{ ref('mart_proc_family') }} f on f.family_id = p.family_id
group by all
order by all
