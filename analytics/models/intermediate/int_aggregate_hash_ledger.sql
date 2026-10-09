-- uid_retired / uid_alias rows as sep entities
-- depends_on: {{ ref('mart_uid_retired') }}
-- depends_on: {{ ref('mart_uid_alias') }}
{{ aggregate_hashes(
  ['uid_retired', 'uid_alias'],
  relations={
    'uid_retired': ref('mart_uid_retired'),
    'uid_alias': ref('mart_uid_alias'),
  }
) }}
