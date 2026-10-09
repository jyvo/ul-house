{#- std columns for gate__ test select -#}
{% macro gate_row(entity_type, entity_key, entry_kind, is_new_item='false', affected='1', runs='null', detail='null') -%}
  cast({{ entity_type }} as varchar) as entity_type,
  cast({{ entity_key }} as varchar) as entity_key,
  cast({{ entry_kind }} as varchar) as entry_kind,
  cast({{ is_new_item }} as boolean) as is_new_item,
  cast({{ affected }} as bigint) as affected,
  cast({{ runs }} as bigint) as runs,
  cast({{ detail }} as varchar) as detail
{%- endmacro %}
