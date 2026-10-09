select uid, name, name_variants from {{ ref('int_weapon_ability') }} where name_variants > 1
