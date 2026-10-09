select
  cast(src."uid" as varchar) as "uid",
  cast(src."infusion_count" as bigint) as "infusion_count",
  cast(src."proc_id" as bigint) as "proc_id"
from (
  select d.source_uid as uid, d.infusion_count, p.proc_id
  from {{ ref('stg_defensive_gear') }} d
  left join {{ ref('int_proc') }} p on p.source_uid = d.source_uid
) src
