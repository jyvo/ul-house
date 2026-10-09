select trim(c.old_uid) as old_uid, trim(c.new_uid) as new_uid
from {{ ref('confirmed_aliases') }} c
where c.old_uid is not null
  and trim(c.old_uid) not in (select old_uid from {{ source('prev', 'uid_alias') }})
  and trim(c.old_uid) not in (select old_uid from {{ ref('int_alias_new') }})
