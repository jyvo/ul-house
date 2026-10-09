-- every effect occurrence (skill effects + potential levels), clean_text, with its effect_id
with lines as (
  select source_uid, owner_kind, owner_ordinal, ordinal, target, description
  from {{ ref('stg_skill_effect') }}
  union all
  select source_uid, 'potential_level', level, 1, target, description
  from {{ ref('stg_potential_level') }}
),
cleaned as (
  select source_uid, owner_kind, owner_ordinal, ordinal,
         {{ clean_text('target') }} as target,
         {{ clean_text('description', false) }} as description
  from lines
)
select *, {{ hash_id('target', 'description') }} as effect_id
from cleaned
