select
  cast(src."element_id" as varchar) as "element_id"
from (
  select distinct element_id from {{ ref('stg_element') }}
) src
