select {{ gate_row("'unlisted_release'", 'u.item_id', "'catalog'", 'false', '1', 'u.runs', 'u.name') }}
from {{ ref('stg_unlisted_release') }} u
cross join {{ ref('int_run') }} r
left join {{ ref('stg_crawl_run') }} c on c.run_id = r.last_complete_run
where u.last_run_id = r.last_complete_run or coalesce(c.new_release_skipped, 0) = 1
