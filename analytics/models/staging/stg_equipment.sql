select * from {{ source('records', 'stg_equipment') }}
