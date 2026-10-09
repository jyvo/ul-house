select * from {{ source('records', 'stg_proc_scaling') }}
