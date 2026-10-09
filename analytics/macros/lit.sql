{#- check var render-#}
{% macro lit(value) -%}
  {%- if value is none -%}null
  {%- elif value is sameas true -%}true
  {%- elif value is sameas false -%}false
  {%- elif value is number -%}{{ value }}
  {%- else -%}'{{ (value | string) | replace("'", "''") }}'
  {%- endif -%}
{%- endmacro %}

{#- ISO-8601 Z text parse to TIMESTAMP (naive UTC)-#}
{% macro iso_ts(expr) -%}
  try_strptime({{ expr }}, '%Y-%m-%dT%H:%M:%SZ')
{%- endmacro %}

{#- entity type -#}
{% macro registry_flags() -%}
  {%- set agg = var('aggregates', none) or [] -%}
  {%- if agg -%}
  select * from (values
    {%- for e in agg %}
    ('{{ e.entity_type }}', {{ 'true' if e.deletable else 'false' }}){% if not loop.last %},{% endif %}
    {%- endfor %}
  ) v(entity_type, deletable)
  {%- else -%}
  select cast(null as varchar) as entity_type, false as deletable where false
  {%- endif -%}
{%- endmacro %}
