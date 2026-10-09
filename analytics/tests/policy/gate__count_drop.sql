with prev as (
  select p.value
  from {{ source('prev', 'population') }} p
  join {{ ref('int_run') }} r on p.run_id = r.prev_release_run_id
  where p.scope = 'catalog'
),
cur as (select value from {{ ref('int_population') }} where scope = 'catalog')
select {{ gate_row("'equipment'", "'*'", "'catalog'", 'false', 'prev.value - cur.value', 'null',
                   "'live catalog ' || prev.value || ' -> ' || cur.value") }}
from prev cross join cur
where prev.value > cur.value
