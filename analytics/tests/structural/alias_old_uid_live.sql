select trim(c.old_uid) as old_uid, trim(c.new_uid) as new_uid
from {{ ref('confirmed_aliases') }} c
join {{ ref('mart_equipment') }} e on e.uid = trim(c.old_uid) and e.state = 'live'
where trim(c.old_uid) not in (select old_uid from {{ source('prev', 'uid_alias') }})
