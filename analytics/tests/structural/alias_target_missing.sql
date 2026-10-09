select trim(old_uid) as old_uid, trim(new_uid) as new_uid
from {{ ref('confirmed_aliases') }}
where new_uid is not null
  and trim(new_uid) not in (select uid from {{ ref('mart_equipment') }})
