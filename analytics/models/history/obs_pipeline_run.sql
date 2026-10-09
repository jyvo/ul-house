-- pipeline layer: one row per transform run from seed's last complete crawl run
with c as (
  select cr.* from {{ ref('stg_crawl_run') }} cr
  join {{ ref('int_run') }} r on cr.run_id = r.last_complete_run
),
s as (
  select r.run_id, r.last_complete_run as crawl_run_id,
         coalesce(c.status, 'missing') as crawl_status,
         date_diff('second', {{ iso_ts('c.started_at') }}, {{ iso_ts('c.ended_at') }}) as crawl_seconds,
         c.pages_discovered, c.pages_fetched, c.pages_changed, c.pages_failed, c.pages_not_modified,
         c.pages_not_modified / nullif(c.pages_fetched, 0) as rate_304,
         c.icons_fetched, c.icons_changed, c.icons_failed,
         (select count(*) from {{ ref('stg_parse_error') }}) as parse_failures,
         r.parser_version,
         (select count(*) from {{ ref('stg_equipment') }}) as equipment_records,
         r.started_at as observed_at
  from {{ ref('int_run') }} r
  left join c on true
)
select run_id, cast(crawl_run_id as bigint) as crawl_run_id, crawl_status,
       cast(crawl_seconds as double) as crawl_seconds,
       cast(pages_discovered as bigint) as pages_discovered, cast(pages_fetched as bigint) as pages_fetched,
       cast(pages_changed as bigint) as pages_changed, cast(pages_failed as bigint) as pages_failed,
       cast(pages_not_modified as bigint) as pages_not_modified, cast(rate_304 as double) as rate_304,
       cast(icons_fetched as bigint) as icons_fetched, cast(icons_changed as bigint) as icons_changed,
       cast(icons_failed as bigint) as icons_failed, cast(parse_failures as bigint) as parse_failures,
       cast(parser_version as bigint) as parser_version, cast(equipment_records as bigint) as equipment_records,
       observed_at
from s
{% if is_incremental() %}
where not exists (select 1 from {{ this }} t where t.run_id = s.run_id)
{% endif %}
