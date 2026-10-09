select
  cast(src."uid" as varchar) as "uid",
  cast(src."level" as bigint) as "level",
  cast(src."effect_id" as bigint) as "effect_id"
from (
  select source_uid as uid, owner_ordinal as level, effect_id
  from {{ ref('int_effect_line') }} where owner_kind = 'potential_level'
) src
