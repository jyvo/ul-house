select source_uid, kind, ordinal, ref_uid, ref_name
from {{ ref('int_evolution_material') }}
where not attributed
