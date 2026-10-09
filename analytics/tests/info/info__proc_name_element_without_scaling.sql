select distinct proc_id, raw_name, name_element
from {{ ref('int_proc') }}
where name_element is not null and scaling_elements = 0
