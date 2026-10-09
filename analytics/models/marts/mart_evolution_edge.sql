select
  cast(src."kind" as varchar) as "kind",
  cast(src."from_uid" as varchar) as "from_uid",
  cast(src."to_uid" as varchar) as "to_uid",
  cast(src."evidence" as varchar) as "evidence",
  cast(src."from_name" as varchar) as "from_name",
  cast(src."to_name" as varchar) as "to_name"
from (
  select kind, from_uid, to_uid, evidence, from_name, to_name from {{ ref('int_evolution_edge') }}
) src
