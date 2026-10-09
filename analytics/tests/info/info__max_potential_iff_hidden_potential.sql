with m as (
  select mo.uid,
         exists (select 1 from {{ ref('mart_stat') }} s where s.uid = mo.uid and s.tier in ('max1', 'max2')) as split,
         exists (select 1 from {{ ref('mart_potential_level') }} p where p.uid = mo.uid) as potential
  from {{ ref('mart_monster') }} mo
)
select uid, split, potential from m where split <> potential
