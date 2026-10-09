select
  cast(src."uid" as varchar) as "uid",
  cast(src."label" as varchar) as "label",
  cast(src."tier" as varchar) as "tier",
  cast(src."value" as bigint) as "value"
from (
  select source_uid as uid, {{ clean_text('label', false) }} as label, {{ clean_text('tier', false) }} as tier, value
  from {{ ref('stg_stat') }}
) src
