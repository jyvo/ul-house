with passives as (
  select source_uid, {{ clean_text('name', false) }} as name, effect_hash
  from {{ ref('stg_passive_skill') }}
)
select source_uid, {{ hash_id('name', 'effect_hash') }} as passive_id, name, effect_hash
from passives
