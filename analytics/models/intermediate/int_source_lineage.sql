select
  e.source_uid as uid,
  p.url as source_url,
  p.html_sha256 as source_hash,
  p.etag,
  p.fetched_at as retrieved_at,
  p.run_id as crawl_run_id,
  f.keep_reason,
  f.entry_kind,
  f.depth,
  f.parent_id
from {{ ref('stg_equipment') }} e
join {{ ref('stg_page') }} p on p.item_id = e.source_uid
left join {{ ref('int_scope_member') }} f on f.uid = e.source_uid
