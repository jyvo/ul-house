select p.old_uid, p.new_uid as ledger_new_uid, trim(c.new_uid) as csv_new_uid
from {{ source('prev', 'uid_alias') }} p
join {{ ref('confirmed_aliases') }} c on trim(c.old_uid) = p.old_uid
where trim(c.new_uid) <> p.new_uid
