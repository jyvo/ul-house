select
  cast(src."old_uid" as varchar) as "old_uid",
  cast(src."new_uid" as varchar) as "new_uid",
  cast(src."since_revision" as bigint) as "since_revision",
  cast(src."confirmed_commit" as varchar) as "confirmed_commit"
from (
  select old_uid, new_uid, since_revision, confirmed_commit from {{ source('prev', 'uid_alias') }}
  union all
  select old_uid, new_uid, since_revision, confirmed_commit from {{ ref('int_alias_new') }}
) src
