select s.uid, s.since_revision, s.reason
from {{ ref('int_retired_new') }} s
{% if is_incremental() %}
where not exists (select 1 from {{ this }} t where t.uid = s.uid)
{% endif %}
