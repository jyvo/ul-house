select 'uid_retired' as ledger, p.uid as entity_key
from {{ source('prev', 'uid_retired') }} p
where not exists (select 1 from {{ ref('mart_uid_retired') }} m
                  where m.uid = p.uid and m.since_revision = p.since_revision and m.reason = p.reason)
union all
select 'uid_alias', p.old_uid
from {{ source('prev', 'uid_alias') }} p
where not exists (select 1 from {{ ref('mart_uid_alias') }} m
                  where m.old_uid = p.old_uid and m.new_uid = p.new_uid
                    and m.since_revision = p.since_revision and m.confirmed_commit = p.confirmed_commit)
