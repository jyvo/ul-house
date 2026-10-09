select * from {{ source('records', 'stg_evolution') }}
