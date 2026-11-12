import json
from datetime import UTC, datetime, timedelta

import duckdb
import pytest
import release_fixtures as F
from release_fixtures import FlakyStore, cleanup_tmp  # noqa: F401

from ul_house.build import checkpoint
from ul_house.build.checkpoint import CheckpointError, run_checkpoint
from ul_house.build.store import ImmutableConflict, Layout, LocalStore, ObjectMissing, StoreError, sha256_file

NOW = datetime(2027, 4, 2, 3, 0, 0, tzinfo=UTC)
CUTOFF = "2027-01-02T03:00:00Z"
OLD, NEWER = "2026-09-01T00:00:00Z", "2027-03-20T00:00:00Z"
MIN_REVISION = 6
CHECKPOINT_KEYS = {"checkpoint_id", "revision", "cutoff", "cutoff_revision", "current_sha256", "archives"}


def clock():
    return NOW


def make_history(path):
    """A history: snapshot, change log, ledger."""
    F.new_history(path)
    conn = duckdb.connect(str(path))
    try:
        for rev in range(1, 11):
            for k in range(1, 4):
                conn.execute("INSERT INTO dataset_changes VALUES (?, 'equipment', ?, 'INSERT', 'h', ?)",
                             [rev, f"{rev}0{k}", OLD])
                conn.execute("INSERT INTO row_version VALUES ('equipment', ?, 'h', ?)", [f"{rev}0{k}", rev])
        for rev in range(1, 11):
            conn.execute("INSERT INTO entity_current VALUES ('equipment', ?, 'h', 'h', ?, 'INSERT', NULL, NULL, ?)",
                         [f"{rev}01", rev, OLD])
        conn.execute("INSERT INTO uid_retired VALUES ('1001', 2, 'gone'), ('1002', 3, 'gone')")
        conn.execute("INSERT INTO uid_alias VALUES ('1001', '1003', 4, ?)", [F.OTHER_COMMIT])
        for rev in range(1, 11):
            conn.execute("INSERT INTO transform_run VALUES (" + ",".join("?" * 19) + ")",
                         [f"t{rev}", F.COMMIT, 7, F.CATALOG_VERSION, F.COMMIT, F.POLICY_VERSION, "d", "m", OLD, OLD,
                          rev, False, F.COMMIT, False, rev, "s", True, None, None])
        for uid, valid_to in (("a", None), ("b", None), ("c", None), ("d", "2026-12-01T00:00:00Z"),
                              ("e", "2026-12-15T00:00:00Z"), ("f", "2027-02-01T00:00:00Z"),
                              ("g", "2027-03-01T00:00:00Z")):
            conn.execute("INSERT INTO source_lineage VALUES (?, 'u', 'h', 'e', ?, '1', 'cost_band', 'catalog', 0, "
                         "NULL, ?, ?)", [uid, OLD, OLD, valid_to])
        for i, observed in enumerate([OLD] * 4 + [NEWER] * 3):
            conn.execute("INSERT INTO obs_data_quality VALUES ('r', 'm', ?, 1.0, NULL, NULL, ?)", [f"s{i}", observed])
        for rev in range(1, 4):
            conn.execute("INSERT INTO release VALUES (?, 't', '1', 's', 's', '{}', '{}', 'm', 'sig', ?, 1, false, "
                         "'pass', 'r')", [rev, OLD])
    finally:
        conn.close()
    return path


def counts_of(path):
    conn = duckdb.connect(str(path), read_only=True)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()]
        return {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path):
    original = make_history(tmp_path / "original.duckdb")
    inner = LocalStore(tmp_path / "store")
    store = FlakyStore(inner)
    store.put(Layout.CURRENT, json.dumps({"revision": 10, "minimum_revision": MIN_REVISION}).encode())
    store.put_file(Layout.HISTORY_CURRENT, original)
    store.log.clear()
    return store, original, tmp_path / "work"


def download(store, key, tmp_path, name):
    dest = tmp_path / "dl" / name
    store.get_file(key, dest)
    return dest


def test_row_counts_reconcile_per_table(env):
    store, original, work = env
    before = counts_of(original)
    result = run_checkpoint(store, work, clock=clock)
    assert set(result.counts) == set(before)
    for table, c in result.counts.items():
        assert c["before"] == before[table]
        assert c["before"] == c["after"] + c["archived"], table
    assert result.counts["dataset_changes"] == {"before": 30, "after": 15, "archived": 15}
    assert result.counts["row_version"] == {"before": 30, "after": 15, "archived": 15}
    assert result.counts["source_lineage"] == {"before": 7, "after": 5, "archived": 2}
    assert result.counts["obs_data_quality"] == {"before": 7, "after": 3, "archived": 4}           # older than 90 days
    reconcile_after = counts_of(download(store, Layout.HISTORY_CURRENT, env[2].parent, "after.duckdb"))
    assert reconcile_after == {t: c["after"] for t, c in result.counts.items()}


def test_ledger_transform_run_release_and_current_state_are_never_archived(env):
    store, original, work = env
    result = run_checkpoint(store, work, clock=clock)
    for table in ("uid_retired", "uid_alias", "transform_run", "release", "entity_current"):
        c = result.counts[table]
        assert c["archived"] == 0 and c["after"] == c["before"], table
    assert result.counts["uid_retired"]["after"] == 2


def test_open_scd2_rows_and_recent_rows_stay(env):
    store, original, work = env
    run_checkpoint(store, work, clock=clock)
    path = download(store, Layout.HISTORY_CURRENT, work.parent, "after.duckdb")
    lineage = F.history_rows(path, "SELECT uid FROM source_lineage ORDER BY uid")
    assert [r[0] for r in lineage] == ["a", "b", "c", "f", "g"]                      # open + closed after the cutoff
    revisions = F.history_rows(path, "SELECT DISTINCT revision FROM dataset_changes ORDER BY 1")
    assert [r[0] for r in revisions] == [6, 7, 8, 9, 10]                             # >= minimum_revision
    scopes = F.history_rows(path, "SELECT scope FROM obs_data_quality ORDER BY scope")
    assert [r[0] for r in scopes] == ["s4", "s5", "s6"]


def test_obs_days_moves_the_cutoff(env):
    store, original, work = env
    result = run_checkpoint(store, work, clock=clock, obs_days=5)
    assert result.counts["obs_data_quality"]["archived"] == 7


def test_the_archive_holds_exactly_the_rows_removed(env, tmp_path):
    store, original, work = env
    result = run_checkpoint(store, work, clock=clock)
    assert result.archive_name == "2027-Q2"
    arc = download(store, Layout.archive("2027-Q2"), tmp_path, "arc.duckdb")
    assert F.history_rows(arc, "SELECT count(*) FROM dataset_changes") == [(15,)]
    assert F.history_rows(arc, "SELECT min(revision), max(revision) FROM dataset_changes") == [(1, 5)]
    assert sorted(r[0] for r in F.history_rows(arc, "SELECT uid FROM source_lineage")) == ["d", "e"]
    removed = F.history_rows(original, "SELECT * FROM dataset_changes WHERE revision < 6 ORDER BY 1, 3")
    assert F.history_rows(arc, "SELECT * FROM dataset_changes ORDER BY 1, 3") == removed
    tables = {r[0] for r in F.history_rows(arc, "SELECT table_name FROM information_schema.tables "
                                                "WHERE table_schema = 'main'")}
    assert tables == {"dataset_changes", "row_version", "source_lineage", "obs_data_quality", "obs_approval"}
    assert result.archive_sha256 == store.head(Layout.archive("2027-Q2")).sha256 == sha256_file(arc)


def test_a_second_run_in_the_quarter_never_overwrites_the_first_archive(env):
    store, original, work = env
    first = run_checkpoint(store, work, clock=clock)
    sha_before = store.head(Layout.archive("2027-Q2")).sha256
    store.put_file(Layout.HISTORY_CURRENT, original)
    second = run_checkpoint(store, work, clock=lambda: NOW + timedelta(hours=1))
    assert second.archive_name == "2027-Q2-2"
    assert store.head(Layout.archive("2027-Q2")).sha256 == sha_before == first.archive_sha256
    assert store.head(Layout.archive("2027-Q2-2")) is not None


def test_put_immutable_refuses_a_different_archive_under_an_existing_name(env):
    from ul_house.build.store import put_immutable
    store, original, work = env
    run_checkpoint(store, work, clock=clock)
    with pytest.raises(ImmutableConflict):
        put_immutable(store, Layout.archive("2027-Q2"), b"a different archive")


def test_nothing_to_archive_writes_no_archive_but_still_rewrites_current(env, tmp_path):
    store, original, work = env
    run_checkpoint(store, work, clock=clock)
    store.log.clear()
    second = run_checkpoint(store, work, clock=lambda: NOW + timedelta(hours=1))
    assert second.archive_name is None and second.archive_sha256 is None
    assert all(c["archived"] == 0 for c in second.counts.values())
    assert not [k for k in store.puts() if k.startswith("build/history/archived/")]
    assert Layout.HISTORY_CURRENT in store.puts() and store.puts()[-1] == Layout.CHECKPOINT
    assert {i.key for i in store.list("build/history/archived/")} == {Layout.archive("2027-Q2")}


# backup and pointer
def test_the_untouched_copy_is_kept_as_a_backup(env):
    store, original, work = env
    result = run_checkpoint(store, work, clock=clock)
    backup = store.head("build/history/backups/pre-20270402T030000Z.duckdb")
    assert result.checkpoint_id == "20270402T030000Z"
    assert backup is not None and backup.sha256 == sha256_file(original)


def test_the_pointer_file_is_the_last_put(env):
    store, original, work = env
    run_checkpoint(store, work, clock=clock)
    puts = store.puts()
    assert puts[-1] == Layout.CHECKPOINT and puts.count(Layout.CHECKPOINT) == 1
    assert puts.index(Layout.archive("2027-Q2")) < puts.index(Layout.HISTORY_CURRENT) < puts.index(Layout.CHECKPOINT)
    assert puts[0] == "build/history/backups/pre-20270402T030000Z.duckdb"


def test_a_failed_archive_upload_leaves_current_and_the_pointer_unchanged(env):
    store, original, work = env
    current_before = store.head(Layout.HISTORY_CURRENT).sha256
    store.fail_on = lambda op, key, i: op == "put" and key.startswith("build/history/archived/")
    with pytest.raises(StoreError):
        run_checkpoint(store, work, clock=clock)
    assert store.head(Layout.HISTORY_CURRENT).sha256 == current_before
    assert store.head(Layout.CHECKPOINT) is None


def test_a_failed_pointer_leaves_the_archive_and_the_new_current_consistent(env, tmp_path):
    store, original, work = env
    store.fail_on = lambda op, key, i: op == "put" and key == Layout.CHECKPOINT
    with pytest.raises(StoreError):
        run_checkpoint(store, work, clock=clock)
    assert store.head(Layout.CHECKPOINT) is None
    assert store.head(Layout.archive("2027-Q2")) is not None
    after = counts_of(download(store, Layout.HISTORY_CURRENT, tmp_path, "after.duckdb"))
    archived = counts_of(download(store, Layout.archive("2027-Q2"), tmp_path, "arc.duckdb"))
    before = counts_of(original)
    for table in ("dataset_changes", "row_version", "source_lineage", "obs_data_quality"):
        assert after[table] + archived[table] == before[table], table


def test_a_wrong_rule_fails_reconciliation_and_uploads_nothing(env, monkeypatch):
    store, original, work = env
    current_before = store.head(Layout.HISTORY_CURRENT).sha256

    def bad_rules(conn, *, cutoff_ts, cutoff_revision):
        # archive table missing: counts disagree
        return [checkpoint.ArchiveRule(
            "dataset_changes",
            "revision < 6 AND NOT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_catalog = 'arc' AND table_name = 'dataset_changes')")]
    monkeypatch.setattr(checkpoint, "archive_rules", bad_rules)
    with pytest.raises(CheckpointError):
        run_checkpoint(store, work, clock=clock)
    puts = store.puts()
    assert not [k for k in puts if k.startswith("build/history/archived/")]
    assert Layout.HISTORY_CURRENT not in puts and Layout.CHECKPOINT not in puts
    assert store.head(Layout.HISTORY_CURRENT).sha256 == current_before


def test_reconcile_function():
    checkpoint.reconcile({"t": {"before": 10, "after": 4, "archived": 6}, "u": {"before": 3, "after": 3, "archived": 0}})
    for bad in ({"before": 10, "after": 4, "archived": 5}, {"before": 3, "after": 2, "archived": 0}):
        with pytest.raises(CheckpointError):
            checkpoint.reconcile({"t": bad})


def test_the_rewritten_current_has_the_reconciled_counts_and_is_not_larger(env, tmp_path):
    store, original, work = env
    size_before = store.head(Layout.HISTORY_CURRENT).size
    result = run_checkpoint(store, work, clock=clock)
    head = store.head(Layout.HISTORY_CURRENT)
    assert head.sha256 == result.current_sha256 and head.size <= size_before
    assert counts_of(download(store, Layout.HISTORY_CURRENT, tmp_path, "now.duckdb")) == {
        t: c["after"] for t, c in result.counts.items()}


def test_checkpoint_json_shape_and_accumulation(env, tmp_path):
    store, original, work = env
    first = run_checkpoint(store, work, clock=clock)
    doc = json.loads(store.get(Layout.CHECKPOINT))
    assert set(doc) == CHECKPOINT_KEYS
    assert doc["checkpoint_id"] == "20270402T030000Z" and doc["revision"] == 10
    assert doc["cutoff"] == CUTOFF and doc["cutoff_revision"] == MIN_REVISION
    assert doc["current_sha256"] == first.current_sha256
    (archive,) = doc["archives"]
    assert archive["name"] == "archived/2027-Q2.duckdb" and archive["sha256"] == first.archive_sha256
    assert archive["row_counts"] == {"dataset_changes": 15, "row_version": 15, "source_lineage": 2,
                                     "obs_data_quality": 4}
    raw = store.get(Layout.CHECKPOINT)
    assert raw == json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    store.put_file(Layout.HISTORY_CURRENT, original)
    second = run_checkpoint(store, work, clock=lambda: NOW + timedelta(hours=1))
    doc = json.loads(store.get(Layout.CHECKPOINT))
    assert [a["name"] for a in doc["archives"]] == ["archived/2027-Q2.duckdb", "archived/2027-Q2-2.duckdb"]
    assert doc["archives"][0]["sha256"] == first.archive_sha256 and doc["archives"][1]["sha256"] == second.archive_sha256


def test_checkpoint_without_a_current_json_uses_revision_zero(tmp_path):
    original = make_history(tmp_path / "o.duckdb")
    store = LocalStore(tmp_path / "store")
    store.put_file(Layout.HISTORY_CURRENT, original)
    result = run_checkpoint(store, tmp_path / "work", clock=clock)
    assert result.counts["dataset_changes"]["archived"] == 0


def test_checkpoint_without_a_history_fails(tmp_path):
    store = LocalStore(tmp_path / "store")
    with pytest.raises(ObjectMissing):
        run_checkpoint(store, tmp_path / "work", clock=clock)


# rules
def test_archive_rules_are_generic(tmp_path):
    path = make_history(tmp_path / "h.duckdb")
    conn = duckdb.connect(str(path))
    try:
        rules = {r.table: r.predicate for r in checkpoint.archive_rules(
            conn, cutoff_ts=CUTOFF, cutoff_revision=MIN_REVISION)}
    finally:
        conn.close()
    assert set(rules) == {"dataset_changes", "row_version", "source_lineage", "obs_data_quality", "obs_approval"}
    assert "revision < 6" in rules["dataset_changes"] and "revision < 6" in rules["row_version"]
    assert "dbt_valid_to IS NOT NULL" in rules["source_lineage"] and CUTOFF in rules["source_lineage"]
    assert "observed_at" in rules["obs_data_quality"] and CUTOFF in rules["obs_data_quality"]
    for never in checkpoint.ARCHIVE_NEVER + ("entity_current",):
        assert never not in rules


def test_a_new_obs_table_and_a_new_scd2_table_are_picked_up_without_code_changes(tmp_path):
    path = make_history(tmp_path / "h.duckdb")
    conn = duckdb.connect(str(path))
    try:
        conn.execute("CREATE TABLE obs_gate_finding (run_id VARCHAR, observed_at VARCHAR)")
        conn.execute("CREATE TABLE snap_thing (k VARCHAR, dbt_valid_to VARCHAR)")
        conn.execute("CREATE TABLE plain_thing (k VARCHAR)")
        rules = {r.table for r in checkpoint.archive_rules(conn, cutoff_ts=CUTOFF, cutoff_revision=MIN_REVISION)}
    finally:
        conn.close()
    assert {"obs_gate_finding", "snap_thing"} <= rules and "plain_thing" not in rules


def test_the_constants_follow_the_proposal():
    assert checkpoint.OBS_DAYS == 90
    assert set(checkpoint.ARCHIVE_NEVER) == {"uid_retired", "uid_alias", "alias_candidate", "alias_rejected",
                                             "transform_run", "release"}
