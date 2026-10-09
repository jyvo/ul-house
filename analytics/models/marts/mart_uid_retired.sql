select
  cast(src."uid" as varchar) as "uid",
  cast(src."since_revision" as bigint) as "since_revision",
  cast(src."reason" as varchar) as "reason"
from (
  select uid, since_revision, reason from {{ source('prev', 'uid_retired') }}
  union all
  select uid, since_revision, reason from {{ ref('int_retired_new') }}
) src
