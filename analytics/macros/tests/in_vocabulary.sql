{% test in_vocabulary(model, column_name, domain) %}
select {{ column_name }} as value, count(*) as n
from {{ model }}
where {{ column_name }} is not null
  and cast({{ column_name }} as varchar) not in (
    select value from {{ ref('vocabulary') }} where domain = '{{ domain }}')
group by 1
{% endtest %}
