{% macro hash_id() -%}
  cast(('0x' || substr(sha256(
    {%- for f in varargs -%}
      coalesce(cast({{ f }} as varchar), ''){% if not loop.last %} || chr(31) || {% endif %}
    {%- endfor -%}
  ), 1, 16))::ubigint & 9223372036854775807::ubigint as bigint)
{%- endmacro %}
