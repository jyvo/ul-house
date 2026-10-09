with base as (
  select e.source_uid as uid,
         coalesce(ps.first_seen, f.discovered_at, pg.fetched_at, r.started_at) as first_seen,
         coalesce(ps.first_revision, r.revision) as first_revision,
         ps.uid is null as is_new
  from {{ ref('stg_equipment') }} e
  cross join {{ ref('int_run') }} r
  left join {{ source('prev', 'equipment_seen') }} ps on ps.uid = e.source_uid
  left join {{ ref('int_scope_member') }} f on f.uid = e.source_uid
  left join {{ ref('stg_page') }} pg on pg.item_id = e.source_uid
)
select b.*,
       (not r.bootstrap
        and {{ iso_ts('b.first_seen') }} >= {{ iso_ts('r.started_at') }} - to_days(cast({{ var('new_item_days') }} as integer))
       ) as is_new_item
from base b
cross join {{ ref('int_run') }} r
