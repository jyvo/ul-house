select s.old_uid, s.new_uid, s.since_revision, s.confirmed_commit
from {{ ref('int_alias_new') }} s
{% if is_incremental() %}
where not exists (select 1 from {{ this }} t where t.old_uid = s.old_uid)
{% endif %}
