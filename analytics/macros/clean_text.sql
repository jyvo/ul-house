{#- sanitize text (whitespace, stripped, trimmed) -#}
{% macro clean_text(expr, empty_as_null=true) -%}
  {%- set cleaned -%}
    trim(regexp_replace(regexp_replace(cast({{ expr }} as varchar), '[\s\p{Z}]+', ' ', 'g'), '[\p{Cc}\p{Cf}]', '', 'g'))
  {%- endset -%}
  {%- if empty_as_null -%}
    nullif({{ cleaned }}, '')
  {%- else -%}
    {{ cleaned }}
  {%- endif -%}
{%- endmacro %}
