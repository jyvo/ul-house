select run_id, trigger, status, started_at, ended_at, pages_discovered, pages_fetched, pages_changed,
       pages_failed, pages_not_modified, icons_fetched, icons_changed, icons_failed, new_release_skipped
from {{ ref('stg_crawl_run') }}
order by run_id
