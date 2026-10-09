with owners as (
  select p.proc_id, min(coalesce(f.entry_kind, 'catalog')) as entry_kind
  from {{ ref('int_proc') }} p left join {{ ref('int_scope_member') }} f on f.uid = p.source_uid
  group by p.proc_id
)
select distinct {{ gate_row("'proc'", 'cast(s.proc_id as varchar)', 'o.entry_kind', 'false', '1', 'null', "'scale_kind ' || s.scale_kind") }}
from {{ ref('mart_proc_scaling') }} s
join owners o on o.proc_id = s.proc_id
where s.scale_kind not in (select value from {{ ref('vocabulary') }} where domain = 'scale_kind')
