select
  cast(src."sha256" as varchar) as "sha256",
  cast(src."kind" as varchar) as "kind",
  cast(src."bytes" as bigint) as "bytes"
from (
  select sha256, kind, bytes from {{ ref('int_icon') }}
) src
