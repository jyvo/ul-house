select seed_fingerprint, catalog_version
from {{ ref('int_run') }}
where seed_fingerprint is distinct from catalog_version
