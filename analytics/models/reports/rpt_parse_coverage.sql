select
  (select count(*) from {{ ref('stg_page') }} where kind = 'detail' and html_sha256 is not null) as detail_pages,
  (select count(*) from {{ ref('stg_equipment') }}) as staged,
  (select count(*) from {{ ref('stg_parse_error') }}) as parse_errors
