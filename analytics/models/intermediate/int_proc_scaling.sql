-- decomp per scaling line
with elements as (
  select element_id from {{ ref('stg_element') }} where element_id <> 'none'
),
procs as (
  select source_uid, {{ hash_id(clean_text('name', false), 'effect_hash') }} as proc_id
  from {{ ref('stg_proc') }}
),
lines as (
  select s.source_uid, p.proc_id, s.ordinal, {{ clean_text('s.description', false) }} as description
  from {{ ref('stg_proc_scaling') }} s
  join procs p on p.source_uid = s.source_uid
),
parsed as (
  select *,
    nullif(regexp_extract(description, 'number of (\S+) elemental', 1), '') as element_token,
    nullif(trim(regexp_replace(lower(regexp_extract(description, '^(.*?)\s+scales with', 1)),
                               '[^a-z0-9]+', '_', 'g'), '_'), '') as scale_token
  from lines
)
select
  source_uid,
  proc_id,
  ordinal,
  coalesce(scale_token, 'unknown') as scale_kind,
  case when element_token in (select element_id from elements) then element_token end as element_id,
  case when description like '%elemental weapons%' then 'weapons' else 'gear' end as gear_kind,
  nullif(regexp_extract(description, 'up to a max of (\S+)', 1), '') as cap,
  try_cast(nullif(regexp_extract(description, 'needed for maximum effect: (\d+)', 1), '') as bigint) as pieces_needed,
  description
from parsed
