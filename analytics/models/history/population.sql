-- history.population
select r.run_id, r.revision, s.scope, s.value, r.started_at as observed_at
from {{ ref('int_population') }} s
cross join {{ ref('int_run') }} r
{% if is_incremental() %}
where not exists (select 1 from {{ this }} t where t.run_id = r.run_id and t.scope = s.scope)
{% endif %}
