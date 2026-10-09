with recursive links as (
  select old_uid, new_uid from {{ source('prev', 'uid_alias') }}
  union
  select trim(old_uid), trim(new_uid) from {{ ref('confirmed_aliases') }}
  where old_uid is not null and new_uid is not null
),
walk(start_uid, uid, path) as (
  select old_uid, new_uid, [old_uid, new_uid] from links
  union all
  select w.start_uid, l.new_uid, list_append(w.path, l.new_uid)
  from walk w join links l on l.old_uid = w.uid
  where w.uid <> w.start_uid and len(w.path) < 1000
    and (l.new_uid = w.start_uid or not list_contains(w.path, l.new_uid))
)
select distinct start_uid from walk where uid = start_uid
