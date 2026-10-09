with refs as (
  select m.ref_uid as uid, coalesce({{ clean_text('m.ref_name', false) }}, '') as name, p.fetched_at, m.source_uid
  from {{ ref('stg_evolution_material') }} m
  left join {{ ref('stg_page') }} p on p.item_id = m.source_uid
  where m.material_kind = 'item' and m.ref_uid is not null
)
select uid, name, count(distinct name) over (partition by uid) as name_variants
from refs
qualify row_number() over (partition by uid order by fetched_at desc nulls last, source_uid desc, name) = 1
