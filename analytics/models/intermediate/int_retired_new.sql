-- first-time retirement rows
select s.uid, r.revision as since_revision, s.retire_reason as reason
from {{ ref('int_equipment_status') }} s
cross join {{ ref('int_run') }} r
where s.state = 'retired'
  and s.uid not in (select uid from {{ source('prev', 'uid_retired') }})
