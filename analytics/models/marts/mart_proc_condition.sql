select
  cast(src."proc_id" as bigint) as "proc_id",
  cast(src."ordinal" as bigint) as "ordinal",
  cast(src."condition" as varchar) as "condition"
from (
  select distinct proc_id, ordinal, condition from {{ ref('int_proc_condition') }}
) src
