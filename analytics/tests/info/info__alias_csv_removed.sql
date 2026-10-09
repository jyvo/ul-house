select p.old_uid, p.new_uid
from {{ source('prev', 'uid_alias') }} p
where p.old_uid not in (select trim(old_uid) from {{ ref('confirmed_aliases') }} where old_uid is not null)
