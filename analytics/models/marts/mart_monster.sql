select
  cast(src."uid" as varchar) as "uid",
  cast(src."passive_id" as bigint) as "passive_id",
  cast(src."restrictions" as varchar) as "restrictions"
from (
  select m.source_uid as uid, ps.passive_id, {{ clean_text('m.restrictions') }} as restrictions
  from {{ ref('stg_monster') }} m
  left join {{ ref('int_passive_skill') }} ps on ps.source_uid = m.source_uid
) src
