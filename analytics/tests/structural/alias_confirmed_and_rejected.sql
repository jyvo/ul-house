select trim(c.old_uid) as old_uid, trim(c.new_uid) as new_uid
from {{ ref('confirmed_aliases') }} c
where exists (select 1 from {{ ref('rejected_aliases') }} j
              where trim(j.old_uid) = trim(c.old_uid) and trim(j.new_uid) = trim(c.new_uid))
   or exists (select 1 from {{ source('prev', 'alias_rejected') }} j
              where j.old_uid = trim(c.old_uid) and j.new_uid = trim(c.new_uid))
