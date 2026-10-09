select * from {{ source('records', 'stg_stat') }}
