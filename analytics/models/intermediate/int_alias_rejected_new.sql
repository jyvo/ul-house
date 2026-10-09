select distinct trim(j.old_uid) as old_uid, trim(j.new_uid) as new_uid, r.revision, r.code_commit as rejected_commit
from {{ ref('rejected_aliases') }} j
cross join {{ ref('int_run') }} r
where j.old_uid is not null and j.new_uid is not null
  and not exists (select 1 from {{ source('prev', 'alias_rejected') }} p
                  where p.old_uid = trim(j.old_uid) and p.new_uid = trim(j.new_uid))
