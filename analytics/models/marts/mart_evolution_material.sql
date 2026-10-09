select
  cast(src."kind" as varchar) as "kind",
  cast(src."from_uid" as varchar) as "from_uid",
  cast(src."to_uid" as varchar) as "to_uid",
  cast(src."ordinal" as bigint) as "ordinal",
  cast(src."material_kind" as varchar) as "material_kind",
  cast(src."ref_uid" as varchar) as "ref_uid",
  cast(src."ref_name" as varchar) as "ref_name",
  cast(src."quantity" as bigint) as "quantity"
from (
  select kind, from_uid, to_uid, ordinal, material_kind, ref_uid, ref_name, quantity
  from {{ ref('int_evolution_material') }} where attributed
) src
