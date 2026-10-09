select
  cast(src."owner_kind" as varchar) as "owner_kind",
  cast(src."owner_id" as varchar) as "owner_id",
  cast(src."ordinal" as bigint) as "ordinal",
  cast(src."effect_id" as bigint) as "effect_id"
from (
  select owner_kind, owner_id, ordinal, effect_id from {{ ref('int_effect_link') }}
) src
