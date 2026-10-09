select uid, name, name_variants from {{ ref('int_item') }} where name_variants > 1
