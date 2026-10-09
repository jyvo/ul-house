-- equip row minus last_changed_revision and last_seen
with icons as (
  select item_id, sha256
  from {{ ref('stg_asset') }}
  where kind = 'equipment' and sha256 is not null
  qualify row_number() over (partition by item_id order by fetched_at desc nulls last, url) = 1
)
select
  e.source_uid as uid,
  {{ clean_text('e.name') }} as name,
  upper({{ clean_text('e.rarity') }}) as rarity,
  {{ clean_text('e.gear_type') }} as gear_type,
  e.cost,
  e.element_id,
  e.max_level,
  i.sha256 as icon_sha,
  coalesce(f.entry_kind, 'reference') as entry_kind,
  coalesce(f.keep_reason, 'unknown') as keep_reason,
  coalesce(f.depth, 0) as evo_depth,
  s.state,
  s.retired_revision,
  fs.first_seen
from {{ ref('stg_equipment') }} e
join {{ ref('int_equipment_status') }} s on s.uid = e.source_uid
join {{ ref('int_first_seen') }} fs on fs.uid = e.source_uid
left join {{ ref('int_scope_member') }} f on f.uid = e.source_uid
left join icons i on i.item_id = e.source_uid
