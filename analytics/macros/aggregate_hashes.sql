{#- one hash per aggregate -#}
{% macro aggregate_hashes(entity_types, relations, exclude={}) -%}
{%- set registry = var('aggregates', none) -%}
{%- if not execute or not registry -%}
select cast(null as varchar) as entity_type, cast(null as varchar) as entity_key,
       cast(null as varchar) as row_hash, cast(null as varchar) as content_hash,
       cast(null as varchar) as state, cast(null as bigint) as retired_revision
where false
{%- else -%}
{%- set columns = {} -%}
{%- for table, rel in relations.items() -%}
  {%- do columns.update({table: adapter.get_columns_in_relation(rel) | map(attribute='name') | list}) -%}
{%- endfor -%}
{%- set selected = [] -%}
{%- for e in registry if e.entity_type in entity_types -%}{%- do selected.append(e) -%}{%- endfor -%}
with
{%- for table, rel in relations.items() %}
"{{ table }}" as (select * from {{ rel }}),
{%- endfor %}
{%- for e in selected %}
{%- set ei = loop.index %}
{%- set key = e.key_columns[0] %}
{%- for c in e.children %}
{%- set bind = ('r."' ~ key ~ '"') if (c.via.key_type if c.via else c.key_type) == 'int' else ('cast(r."' ~ key ~ '" as varchar)') %}
"agg_{{ ei }}_{{ loop.index }}" as (
  select r."{{ key }}" as k,
         string_agg('{{ c.table }}' || chr(30) || {{ row_text('m', columns[c.table]) }}, chr(28)
                    order by '{{ c.table }}' || chr(30) || {{ row_text('m', columns[c.table]) }}) as t
  from "{{ e.root_table }}" r
  {%- if c.via %}
  join "{{ c.via.table }}" v on ({% for oc in c.via.owner_columns %}v."{{ oc }}" = {{ bind }}{% if not loop.last %} or {% endif %}{% endfor %})
  join "{{ c.table }}" m on m."{{ c.owner_columns[0] }}" = ({% for ic in c.via.id_columns %}cast(v."{{ ic }}" as varchar){% if not loop.last %} || ':' || {% endif %}{% endfor %})
    {%- if c.owner_kind %} and m.owner_kind = '{{ c.owner_kind }}'{% endif %}
  {%- else %}
  join "{{ c.table }}" m on ({% for oc in c.owner_columns %}m."{{ oc }}" = {{ bind }}{% if not loop.last %} or {% endif %}{% endfor %})
    {%- if c.owner_kind %} and m.owner_kind = '{{ c.owner_kind }}'{% endif %}
  {%- endif %}
  group by r."{{ key }}"
),
{%- endfor %}
{%- endfor %}
hashes as (
{%- for e in selected %}
{%- set ei = loop.index %}
{%- set key = e.key_columns[0] %}
{%- set root_cols = columns[e.root_table] %}
{%- set dropped = exclude.get(e.entity_type, []) %}
{%- set content_cols = [] %}{% for col in root_cols if col not in dropped %}{% do content_cols.append(col) %}{% endfor %}
{%- set children_text %}{% for c in e.children %} || chr(28) || coalesce("agg_{{ ei }}_{{ loop.index }}".t, ''){% endfor %}{% endset %}
  select '{{ e.entity_type }}' as entity_type,
         cast(r."{{ key }}" as varchar) as entity_key,
         sha256('{{ e.entity_type }}' || chr(29) || '{{ e.root_table }}' || chr(30) || {{ row_text('r', root_cols) }}{{ children_text }}) as row_hash,
         sha256('{{ e.entity_type }}' || chr(29) || '{{ e.root_table }}' || chr(30) || {{ row_text('r', content_cols) }}{{ children_text }}) as content_hash,
         {% if 'state' in root_cols %}cast(r."state" as varchar){% else %}cast(null as varchar){% endif %} as state,
         {% if 'retired_revision' in root_cols %}cast(r."retired_revision" as bigint){% else %}cast(null as bigint){% endif %} as retired_revision
  from "{{ e.root_table }}" r
  {%- for c in e.children %}
  left join "agg_{{ ei }}_{{ loop.index }}" on "agg_{{ ei }}_{{ loop.index }}".k = r."{{ key }}"
  {%- endfor %}
  {% if not loop.last %}union all{% endif %}
{%- endfor %}
)
select * from hashes
{%- endif -%}
{%- endmacro %}
