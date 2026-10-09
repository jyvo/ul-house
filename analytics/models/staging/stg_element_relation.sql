select * from {{ source('records', 'stg_element_relation') }}
