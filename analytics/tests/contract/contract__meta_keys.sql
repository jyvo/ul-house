select k.key
from (values ('seed.schema_version'), ('scope.fingerprint'), ('crawl.last_complete_run')) k(key)
where k.key not in (select key from {{ ref('stg_meta') }})
