select {{ gate_row("'equipment'", 'pe.source_id', "coalesce(f.entry_kind, 'catalog')",
                   "(not r.bootstrap and pe.source_id not in (select uid from " ~ source('prev', 'equipment_seen') ~ "))",
                   '1', 'null', 'pe.error') }}
from {{ ref('stg_parse_error') }} pe
cross join {{ ref('int_run') }} r
left join {{ ref('int_scope_member') }} f on f.uid = pe.source_id
