select f.item_id, f.last_error
from {{ ref('stg_frontier') }} f
join {{ ref('int_run') }} r on f.decided_run_id = r.last_complete_run
where f.last_error like 'contract:%'
