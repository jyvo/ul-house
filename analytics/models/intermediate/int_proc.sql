{%- set tokens = var('proc_size_tokens') | join('|') %}
with elements as (
  select string_agg(element_id, '|' order by element_id) as alt
  from {{ ref('stg_element') }} where element_id <> 'none'
),
procs as (
  select source_uid,
         {{ clean_text('name', false) }} as raw_name,
         effect_hash,
         {{ clean_text('activation_rate') }} as activation_rate
  from {{ ref('stg_proc') }}
),
sized as (
  select *,
    {{ hash_id('raw_name', 'effect_hash') }} as proc_id,
    nullif(regexp_extract(raw_name, '^.*\s({{ tokens }})$', 1), '') as size
  from procs
),
based as (
  select *,
    case when size is null then raw_name
         else trim(regexp_extract(raw_name, '^(.*)\s({{ tokens }})$', 1)) end as base_name
  from sized
),
named as (
  select b.source_uid,
         arg_min(e.element_id, strpos(' ' || b.base_name || ' ', ' ' || e.element_id || ' ')) as name_element
  from based b
  join {{ ref('stg_element') }} e
    on e.element_id <> 'none'
   and strpos(' ' || b.base_name || ' ', ' ' || e.element_id || ' ') > 0
  group by b.source_uid
),
scaling as (
  select source_uid,
         count(distinct element_id) as scaling_elements,
         min(element_id) as scaling_element
  from {{ ref('int_proc_scaling') }}
  group by source_uid
),
decomposed as (
  select b.*,
    n.name_element,
    case when n.name_element is null then null
         when b.base_name = n.name_element or starts_with(b.base_name, n.name_element || ' ') then 'prefix'
         when ends_with(b.base_name, ' ' || n.name_element) then 'suffix'
         else 'infix' end as element_position,
    coalesce(nullif(trim(regexp_replace(
               regexp_replace(' ' || replace(b.base_name, ' ', '  ') || ' ', ' (' || el.alt || ') ', ' ', 'g'),
               '\s+', ' ', 'g')), ''), b.base_name) as family_name,
    coalesce(s.scaling_elements, 0) as scaling_elements,
    case when s.scaling_elements = 1 then s.scaling_element end as scaling_element
  from based b
  cross join elements el
  left join named n on n.source_uid = b.source_uid
  left join scaling s on s.source_uid = b.source_uid
)
select
  source_uid, proc_id, raw_name, effect_hash, activation_rate, size, base_name,
  name_element, element_position, family_name,
  {{ hash_id('family_name') }} as family_id,
  scaling_elements, scaling_element,
  scaling_element as element_id
from decomposed
