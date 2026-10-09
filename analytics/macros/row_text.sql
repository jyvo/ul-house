{% macro row_text(relation_alias, columns) -%}
  {%- for c in columns -%}
    coalesce('v' || cast({{ relation_alias }}."{{ c }}" as varchar), 'n'){% if not loop.last %} || chr(31) || {% endif %}
  {%- endfor -%}
  {%- if not columns -%}''{%- endif -%}
{%- endmacro %}
