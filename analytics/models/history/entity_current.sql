-- latest hash and state per entity
with p as (select * from {{ source('prev', 'entity_current') }}),
c as (select * from {{ ref('int_changes') }} where operation <> 'ALIAS')
select p.entity_type, p.entity_key, p.row_hash, p.content_hash, p.last_revision, p.last_operation,
       p.state, p.retired_revision, p.content_changed_at
from p
where not exists (select 1 from c where c.entity_type = p.entity_type and c.entity_key = p.entity_key)
union all
select c.entity_type, c.entity_key, c.row_hash, c.content_hash, c.revision, c.operation,
       c.state, c.retired_revision,
       case when c.operation in ('INSERT', 'UPDATE') then c.changed_at else p.content_changed_at end
from c
left join p on p.entity_type = c.entity_type and p.entity_key = c.entity_key
