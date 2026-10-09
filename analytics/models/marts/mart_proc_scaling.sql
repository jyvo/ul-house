select
  cast(src."proc_id" as bigint) as "proc_id",
  cast(src."ordinal" as bigint) as "ordinal",
  cast(src."scale_kind" as varchar) as "scale_kind",
  cast(src."element_id" as varchar) as "element_id",
  cast(src."gear_kind" as varchar) as "gear_kind",
  cast(src."cap" as varchar) as "cap",
  cast(src."pieces_needed" as bigint) as "pieces_needed",
  cast(src."description" as varchar) as "description"
from (
  select distinct proc_id, ordinal, scale_kind, element_id, gear_kind, cap, pieces_needed, description from {{ ref('int_proc_scaling') }}
) src
