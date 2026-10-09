select
  cast(src."effect_id" as bigint) as "effect_id",
  cast(src."target" as varchar) as "target",
  cast(src."description" as varchar) as "description"
from (
  select effect_id, target, description from {{ ref('int_skill_effect') }}
) src
