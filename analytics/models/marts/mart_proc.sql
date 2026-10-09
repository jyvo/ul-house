select
  cast(src."proc_id" as bigint) as "proc_id",
  cast(src."family_id" as bigint) as "family_id",
  cast(src."element_id" as varchar) as "element_id",
  cast(src."size" as varchar) as "size",
  cast(src."raw_name" as varchar) as "raw_name",
  cast(src."activation_rate" as varchar) as "activation_rate",
  cast(src."element_position" as varchar) as "element_position"
from (
  select distinct proc_id, family_id, element_id, size, raw_name, activation_rate, element_position from {{ ref('int_proc') }}
) src
