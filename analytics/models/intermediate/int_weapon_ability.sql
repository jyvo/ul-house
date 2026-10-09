with abilities as (
  select a.source_uid, a.uid, {{ clean_text('a.name', false) }} as name, p.fetched_at
  from {{ ref('stg_weapon_ability') }} a
  left join {{ ref('stg_page') }} p on p.item_id = a.source_uid
)
select uid, name, source_uid,
       count(distinct name) over (partition by uid) as name_variants
from abilities
qualify row_number() over (partition by uid order by fetched_at desc nulls last, source_uid desc) = 1
