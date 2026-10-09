select
  cast(src."kind" as varchar) as "kind",
  cast(src."chain_id" as varchar) as "chain_id",
  cast(src."uid" as varchar) as "uid",
  cast(src."position" as bigint) as "position"
from (
  select kind, chain_id, uid, position from {{ ref('int_evolution_chain') }}
) src
