select
  cast(src."uid" as varchar) as "uid",
  cast(src."name" as varchar) as "name"
from (
  select uid, name from {{ ref('int_weapon_ability') }}
) src
