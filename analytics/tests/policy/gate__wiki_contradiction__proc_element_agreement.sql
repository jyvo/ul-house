with owners as (
  select p.proc_id, min(coalesce(f.entry_kind, 'catalog')) as entry_kind   -- 'catalog' < 'reference'
  from {{ ref('int_proc') }} p left join {{ ref('int_scope_member') }} f on f.uid = p.source_uid
  group by p.proc_id
)
select distinct {{ gate_row("'proc'", 'cast(p.proc_id as varchar)', 'o.entry_kind', 'false', '1', 'null',
                   "p.raw_name || ': name ' || p.name_element || ', scaling ' || p.scaling_element") }}
from {{ ref('int_proc') }} p
join owners o on o.proc_id = p.proc_id
where p.name_element is not null and p.scaling_element is not null and p.name_element <> p.scaling_element
