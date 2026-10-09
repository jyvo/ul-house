select {{ hash_id('source_uid', 'ordinal') }} as skill_id,
       source_uid as uid,
       ordinal,
       {{ clean_text('name', false) }} as name
from {{ ref('stg_monster_skill') }}
