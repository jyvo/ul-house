-- first_seen
select s.uid, s.first_seen, s.first_revision
from {{ ref('int_first_seen') }} s
where s.is_new
{% if is_incremental() %}
  and not exists (select 1 from {{ this }} t where t.uid = s.uid)
{% endif %}
