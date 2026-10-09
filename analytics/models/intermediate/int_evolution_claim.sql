with pages as (
  select source_uid, {{ clean_text('name', false) }} as name from {{ ref('stg_equipment') }}
)
select ev.kind, ev.before_uid as from_uid, ev.source_uid as to_uid, 'before' as side, ev.source_uid,
       coalesce({{ clean_text('ev.before_name', false) }}, '') as from_name, p.name as to_name
from {{ ref('stg_evolution') }} ev
join pages p on p.source_uid = ev.source_uid
where ev.before_uid is not null
union all
select ev.kind, ev.source_uid, ev.after_uid, 'after', ev.source_uid, p.name, coalesce({{ clean_text('ev.after_name', false) }}, '')
from {{ ref('stg_evolution') }} ev
join pages p on p.source_uid = ev.source_uid
where ev.after_uid is not null
