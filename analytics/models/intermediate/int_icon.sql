select distinct a.sha256, 'equipment' as kind, a.body_bytes as bytes
from {{ ref('int_ship_equipment') }} s
join {{ ref('stg_asset') }} a on a.sha256 = s.icon_sha and a.kind = 'equipment'
