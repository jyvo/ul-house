-- before / after / both evidence
with claims as (select * from {{ ref('int_evolution_claim') }}),
befores as (select * from claims where side = 'before'),
pairs as (
  select kind, from_uid, to_uid,
         bool_or(side = 'before') as has_before,
         bool_or(side = 'after') as has_after,
         min(from_name) as claimed_from_name,
         min(to_name) as claimed_to_name
  from claims
  group by kind, from_uid, to_uid
),
kept as (
  select p.* from pairs p
  where p.has_before
     or (not exists (select 1 from befores b where b.kind = p.kind and b.from_uid = p.from_uid and b.to_uid <> p.to_uid)
         and not exists (select 1 from befores b where b.kind = p.kind and b.to_uid = p.to_uid and b.from_uid <> p.from_uid))
),
names as (
  select source_uid as uid, {{ clean_text('name', false) }} as name from {{ ref('stg_equipment') }}
)
select k.kind, k.from_uid, k.to_uid,
       case when k.has_before and k.has_after then 'both' when k.has_before then 'before' else 'after' end as evidence,
       coalesce(nf.name, k.claimed_from_name) as from_name,
       coalesce(nt.name, k.claimed_to_name) as to_name
from kept k
left join names nf on nf.uid = k.from_uid
left join names nt on nt.uid = k.to_uid
