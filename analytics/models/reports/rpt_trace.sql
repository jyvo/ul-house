select e.uid, p.url, p.run_id as crawl_run_id, p.html_sha256 as source_hash,
       h.row_hash, c.revision, c.operation, r.run_id as transform_run_id, r.parser_version, r.code_commit
from {{ ref('mart_equipment') }} e
cross join {{ ref('int_run') }} r
left join {{ ref('stg_page') }} p on p.item_id = e.uid
left join {{ ref('int_aggregate_hash_game') }} h on h.entity_type = 'equipment' and h.entity_key = e.uid
left join {{ ref('int_changes') }} c on c.entity_type = 'equipment' and c.entity_key = e.uid
