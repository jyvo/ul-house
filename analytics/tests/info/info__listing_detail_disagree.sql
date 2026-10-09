select l.item_id, l.grp, e.model_class, l.rarity as listing_rarity, e.rarity as detail_rarity
from {{ ref('stg_listing') }} l
join {{ ref('stg_equipment') }} e on e.source_uid = l.item_id
where upper(l.rarity) <> upper(e.rarity)
   or l.grp <> case e.model_class when 'defensive_gear' then 'armor' else e.model_class end
