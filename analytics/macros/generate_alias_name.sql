{#- names with alias wins -#}
{% macro generate_alias_name(custom_alias_name=none, node=none) -%}
  {%- if custom_alias_name -%}
    {{ custom_alias_name | trim }}
  {%- elif node.resource_type == 'model' and node.name.startswith('mart_') -%}
    {{ node.name[5:] }}
  {%- elif node.resource_type == 'model' and node.name.startswith('ledger_') -%}
    {{ node.name[7:] }}
  {%- elif node.version -%}
    {{ node.name ~ '_v' ~ (node.version | replace('.', '_')) }}
  {%- else -%}
    {{ node.name }}
  {%- endif -%}
{%- endmacro %}
