select
  cast(src."uid" as varchar) as "uid",
  cast(src."infusion_count" as bigint) as "infusion_count",
  cast(src."proc_id" as bigint) as "proc_id",
  cast(src."ability_uid" as varchar) as "ability_uid"
from (
  select w.source_uid as uid, w.infusion_count, p.proc_id, a.uid as ability_uid
  from {{ ref('stg_weapon') }} w
  left join {{ ref('int_proc') }} p on p.source_uid = w.source_uid
  left join {{ ref('stg_weapon_ability') }} a on a.source_uid = w.source_uid
) src
