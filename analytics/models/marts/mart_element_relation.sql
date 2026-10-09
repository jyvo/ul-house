select
  cast(src."element_id" as varchar) as "element_id",
  cast(src."related_id" as varchar) as "related_id",
  cast(src."kind" as varchar) as "kind"
from (
  select distinct element_id, related_id, kind from {{ ref('stg_element_relation') }}
) src
