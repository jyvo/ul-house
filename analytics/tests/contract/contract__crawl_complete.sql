select r.last_complete_run
from {{ ref('int_run') }} r
where not exists (select 1 from {{ ref('stg_crawl_run') }} c
                  where c.run_id = r.last_complete_run and c.status = 'complete')
