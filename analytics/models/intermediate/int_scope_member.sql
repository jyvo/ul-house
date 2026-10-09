select
  f.item_id as uid,
  f.source,
  f.entry_kind,
  f.keep_reason,
  f.depth,
  f.parent_id,
  f.state as frontier_state,
  f.last_error,
  f.decided_run_id,
  f.discovered_at,
  coalesce(f.decided_run_id = r.last_complete_run, false) as in_scope
from {{ ref('stg_frontier') }} f
cross join {{ ref('int_run') }} r
