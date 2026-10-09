select
  cast(src."passive_id" as bigint) as "passive_id",
  cast(src."name" as varchar) as "name",
  cast(src."effect_hash" as varchar) as "effect_hash"
from (
  select distinct passive_id, name, effect_hash from {{ ref('int_passive_skill') }}
) src
