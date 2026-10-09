with live as (
  select s.uid, s.entry_kind, f.is_new_item
  from {{ ref('int_ship_equipment') }} s
  join {{ ref('int_first_seen') }} f on f.uid = s.uid
  where s.state = 'live'
)
select scope,
  cast(case scope
    when 'catalog'   then (select count(*) from live where entry_kind = 'catalog')
    when 'reference' then (select count(*) from live where entry_kind = 'reference')
    when 'new_item'  then (select count(*) from live where is_new_item)
  end as bigint) as value
from (values ('catalog'), ('reference'), ('new_item')) v(scope)
