select
  cast(src."family_id" as bigint) as "family_id",
  cast(src."name" as varchar) as "name"
from (
  select distinct family_id, family_name as name from {{ ref('int_proc') }}
) src
