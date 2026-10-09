select s.old_uid, s.new_uid, s.revision, s.rejected_commit
from {{ ref('int_alias_rejected_new') }} s
{% if is_incremental() %}
where not exists (select 1 from {{ this }} t where t.old_uid = s.old_uid and t.new_uid = s.new_uid)
{% endif %}
