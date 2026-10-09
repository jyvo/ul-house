-- seed's unlisted_release rows (curr run)
select r.run_id, u.item_id, u.name, u.runs, r.started_at as observed_at
from {{ ref('stg_unlisted_release') }} u
cross join {{ ref('int_run') }} r
{% if is_incremental() %}
where not exists (select 1 from {{ this }} t where t.run_id = r.run_id and t.item_id = u.item_id)
{% endif %}
