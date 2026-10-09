-- DuckDB types: VARCHAR / BIGINT / DOUBLE / BOOLEAN; times are ISO-8601 UTC text ending in Z

CREATE TABLE IF NOT EXISTS dataset_changes (
  revision BIGINT NOT NULL, entity_type VARCHAR NOT NULL, entity_key VARCHAR NOT NULL,
  operation VARCHAR NOT NULL, row_hash VARCHAR, changed_at VARCHAR NOT NULL,
  PRIMARY KEY (revision, entity_type, entity_key));
CREATE TABLE IF NOT EXISTS row_version (
  entity_type VARCHAR NOT NULL, entity_key VARCHAR NOT NULL, row_hash VARCHAR,
  revision BIGINT NOT NULL, PRIMARY KEY (entity_type, entity_key, revision));
CREATE TABLE IF NOT EXISTS entity_current (
  entity_type VARCHAR NOT NULL, entity_key VARCHAR NOT NULL, row_hash VARCHAR,
  content_hash VARCHAR, last_revision BIGINT NOT NULL, last_operation VARCHAR NOT NULL,
  state VARCHAR, retired_revision BIGINT, content_changed_at VARCHAR,
  PRIMARY KEY (entity_type, entity_key));
CREATE TABLE IF NOT EXISTS uid_retired (
  uid VARCHAR PRIMARY KEY, since_revision BIGINT NOT NULL, reason VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS uid_alias (
  old_uid VARCHAR PRIMARY KEY, new_uid VARCHAR NOT NULL, since_revision BIGINT NOT NULL,
  confirmed_commit VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS alias_candidate (
  old_uid VARCHAR NOT NULL, new_uid VARCHAR NOT NULL, first_seen_revision BIGINT NOT NULL,
  score DOUBLE NOT NULL, evidence VARCHAR NOT NULL, PRIMARY KEY (old_uid, new_uid));
CREATE TABLE IF NOT EXISTS alias_rejected (
  old_uid VARCHAR NOT NULL, new_uid VARCHAR NOT NULL, revision BIGINT NOT NULL,
  rejected_commit VARCHAR NOT NULL, PRIMARY KEY (old_uid, new_uid));
CREATE TABLE IF NOT EXISTS equipment_seen (
  uid VARCHAR PRIMARY KEY, first_seen VARCHAR NOT NULL, first_revision BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS population (
  run_id VARCHAR NOT NULL, revision BIGINT NOT NULL, scope VARCHAR NOT NULL,
  value BIGINT NOT NULL, observed_at VARCHAR NOT NULL, PRIMARY KEY (run_id, scope));
CREATE TABLE IF NOT EXISTS transform_run (                     -- §7.1 columns first, then additions
  run_id VARCHAR PRIMARY KEY, code_commit VARCHAR NOT NULL, parser_version BIGINT NOT NULL,
  catalog_version VARCHAR NOT NULL, catalog_commit VARCHAR NOT NULL, policy_version VARCHAR NOT NULL,
  dbt_invocation_id VARCHAR, dbt_manifest_sha256 VARCHAR, started_at VARCHAR NOT NULL,
  ended_at VARCHAR NOT NULL,
  revision BIGINT NOT NULL, bootstrap BOOLEAN NOT NULL, policy_commit VARCHAR NOT NULL,
  dirty BOOLEAN NOT NULL, crawl_run_id BIGINT NOT NULL, seed_sha256 VARCHAR NOT NULL,
  dbt_success BOOLEAN NOT NULL, prev_release_revision BIGINT, prev_release_run_id VARCHAR,
  transform_version BIGINT);                                   -- P24 TRANSFORM_VERSION (unexplained_churn)
CREATE TABLE IF NOT EXISTS obs_pipeline_run (
  run_id VARCHAR PRIMARY KEY, crawl_run_id BIGINT NOT NULL, crawl_status VARCHAR NOT NULL,
  crawl_seconds DOUBLE, pages_discovered BIGINT, pages_fetched BIGINT, pages_changed BIGINT,
  pages_failed BIGINT, pages_not_modified BIGINT, rate_304 DOUBLE, icons_fetched BIGINT,
  icons_changed BIGINT, icons_failed BIGINT, parse_failures BIGINT NOT NULL,
  parser_version BIGINT NOT NULL, equipment_records BIGINT NOT NULL, observed_at VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS obs_data_quality (
  run_id VARCHAR NOT NULL, metric VARCHAR NOT NULL, scope VARCHAR NOT NULL, value DOUBLE,
  prev_value DOUBLE, delta_pct DOUBLE, observed_at VARCHAR NOT NULL,
  PRIMARY KEY (run_id, metric, scope));
CREATE TABLE IF NOT EXISTS obs_unlisted_release (
  run_id VARCHAR NOT NULL, item_id VARCHAR NOT NULL, name VARCHAR NOT NULL, runs BIGINT NOT NULL,
  observed_at VARCHAR NOT NULL, PRIMARY KEY (run_id, item_id));
CREATE TABLE IF NOT EXISTS obs_gate_decision (
  run_id VARCHAR PRIMARY KEY, revision BIGINT NOT NULL, level VARCHAR NOT NULL,
  policy_version VARCHAR NOT NULL, policy_commit VARCHAR NOT NULL, decision_json VARCHAR NOT NULL,
  observed_at VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS obs_gate_finding (
  run_id VARCHAR NOT NULL, check_name VARCHAR NOT NULL, scope VARCHAR NOT NULL,
  level VARCHAR NOT NULL, count BIGINT NOT NULL, population BIGINT, pct DOUBLE,
  escalated BOOLEAN NOT NULL, consecutive_runs BIGINT NOT NULL, observed_at VARCHAR NOT NULL,
  PRIMARY KEY (run_id, check_name, scope));
