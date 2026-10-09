select
  cast(src."uid" as varchar) as "uid",
  cast(src."name" as varchar) as "name",
  cast(src."rarity" as varchar) as "rarity",
  cast(src."gear_type" as varchar) as "gear_type",
  cast(src."cost" as bigint) as "cost",
  cast(src."element_id" as varchar) as "element_id",
  cast(src."max_level" as bigint) as "max_level",
  cast(src."icon_sha" as varchar) as "icon_sha",
  cast(src."entry_kind" as varchar) as "entry_kind",
  cast(src."keep_reason" as varchar) as "keep_reason",
  cast(src."evo_depth" as bigint) as "evo_depth",
  cast(src."state" as varchar) as "state",
  cast(src."retired_revision" as bigint) as "retired_revision",
  cast(src."first_seen" as varchar) as "first_seen",
  cast(src."last_seen" as varchar) as "last_seen",
  cast(src."last_changed_revision" as bigint) as "last_changed_revision"
from (
  select s.*,
         coalesce(case when c.operation in ('INSERT', 'UPDATE') then r.started_at end,
                  p.content_changed_at, r.started_at) as last_seen,
         coalesce(case when c.operation in ('INSERT', 'UPDATE', 'RETIRE') then r.revision end,
                  p.last_revision, r.revision) as last_changed_revision
  from {{ ref('int_ship_equipment') }} s
  cross join {{ ref('int_run') }} r
  left join {{ ref('int_changes') }} c
    on c.entity_type = 'equipment' and c.entity_key = s.uid and c.operation in ('INSERT', 'UPDATE', 'RETIRE')
  left join {{ source('prev', 'entity_current') }} p
    on p.entity_type = 'equipment' and p.entity_key = s.uid and p.last_operation <> 'DELETE'
) src
