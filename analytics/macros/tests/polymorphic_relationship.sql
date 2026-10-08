{% test polymorphic_relationship(model, column_name, kind_value, to=none, to_field=none) %}
select l.*
from {{ model }} l
where l.owner_kind = '{{ kind_value }}'
{%- if to is not none %}
  and l.{{ column_name }} not in (select cast({{ to_field }} as varchar) from {{ to }} where {{ to_field }} is not null)
{%- endif %}
{% endtest %}
