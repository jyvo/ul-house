select c.source_uid, p.proc_id, c.ordinal, {{ clean_text('c.condition', false) }} as condition
from {{ ref('stg_proc_condition') }} c
join {{ ref('int_proc') }} p on p.source_uid = c.source_uid
