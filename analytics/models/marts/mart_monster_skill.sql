select
  cast(src."skill_id" as bigint) as "skill_id",
  cast(src."uid" as varchar) as "uid",
  cast(src."ordinal" as bigint) as "ordinal",
  cast(src."name" as varchar) as "name"
from (
  select skill_id, uid, ordinal, name from {{ ref('int_monster_skill') }}
) src
