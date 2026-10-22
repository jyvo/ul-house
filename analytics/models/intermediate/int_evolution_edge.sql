-- one edge per `before` claim
with claims as (select * from {{ ref('int_evolution_claim') }}),
names as (
  select source_uid as uid, {{ clean_text('name', false) }} as name from {{ ref('stg_equipment') }}
)
select c.kind, c.from_uid, c.to_uid,
       coalesce(nf.name, c.from_name) as from_name,
       coalesce(nt.name, c.to_name) as to_name
from claims c
left join names nf on nf.uid = c.from_uid
left join names nt on nt.uid = c.to_uid
