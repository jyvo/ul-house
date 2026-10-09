-- unique effect
select distinct effect_id, target, description
from {{ ref('int_effect_line') }}
