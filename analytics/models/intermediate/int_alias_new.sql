-- recorded iff old_uid retired in prev release -> new_uid shipped and live, old_uid no other change this stage
with csv as (
  select distinct trim(old_uid) as old_uid, trim(new_uid) as new_uid
  from {{ ref('confirmed_aliases') }}
  where old_uid is not null and new_uid is not null
),
prev_eq as (
  select entity_key as uid, state, row_hash
  from {{ source('prev', 'entity_current') }}
  where entity_type = 'equipment' and last_operation <> 'DELETE'
),
cur as (
  select entity_key as uid, row_hash from {{ ref('int_aggregate_hash_game') }} where entity_type = 'equipment'
)
select c.old_uid, c.new_uid, r.revision as since_revision, r.code_commit as confirmed_commit
from csv c
cross join {{ ref('int_run') }} r
join prev_eq pe on pe.uid = c.old_uid and pe.state = 'retired'
join cur on cur.uid = c.old_uid and cur.row_hash = pe.row_hash
join {{ ref('int_ship_equipment') }} s on s.uid = c.new_uid and s.state = 'live'
where c.old_uid not in (select old_uid from {{ source('prev', 'uid_alias') }})
  and c.old_uid <> c.new_uid
