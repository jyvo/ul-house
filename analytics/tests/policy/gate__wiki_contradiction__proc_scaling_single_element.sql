with owners as (
  select p.proc_id, min(coalesce(f.entry_kind, 'catalog')) as entry_kind
  from {{ ref('int_proc') }} p left join {{ ref('int_scope_member') }} f on f.uid = p.source_uid
  group by p.proc_id
)
select distinct {{ gate_row("'proc'", 'cast(p.proc_id as varchar)', 'o.entry_kind', 'false', '1', 'null', 'p.raw_name') }}
from {{ ref('int_proc') }} p
join owners o on o.proc_id = p.proc_id
where p.scaling_elements > 1
