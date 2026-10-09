with prev_eq as (
  select entity_key as uid, state, retired_revision
  from {{ source('prev', 'entity_current') }}
  where entity_type = 'equipment' and last_operation <> 'DELETE'
),
reasons as (
  select e.source_uid as uid, e.model_class,
    case when f.frontier_state = 'gone' then 'gone'
         when f.uid is null or f.decided_run_id is null or f.decided_run_id < r.last_complete_run then 'out_of_scope'
    end as retire_reason,
    p.uid is not null as in_prev,
    p.state as prev_state,
    p.retired_revision as prev_retired_revision,
    r.revision
  from {{ ref('stg_equipment') }} e
  cross join {{ ref('int_run') }} r
  left join {{ ref('int_scope_member') }} f on f.uid = e.source_uid
  left join prev_eq p on p.uid = e.source_uid
)
select uid, model_class,
  case when retire_reason is null then 'live' else 'retired' end as state,
  retire_reason,
  case when retire_reason is null then null
       when prev_state = 'retired' then coalesce(prev_retired_revision, revision)
       else revision end as retired_revision,
  (retire_reason is not null and (not in_prev or prev_state = 'live')) as newly_retired,
  (retire_reason is null and coalesce(prev_state = 'retired', false)) as revived,
  prev_state
from reasons
