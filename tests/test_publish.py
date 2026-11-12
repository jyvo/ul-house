"""publish: steps, candidates, heartbeat, check, retention"""
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
import release_fixtures as F
from release_fixtures import FakeS3Client, FlakyStore, World, base_step

from ul_house.build import publish, sign
from ul_house.build.publish import (
    BuildState,
    CandidateMissing,
    CandidateStale,
    DirtyBuild,
    PostPublishCheckFailed,
    PublishError,
    RevisionConflict,
)
from ul_house.build.release import Bundle, canonical_bytes
from ul_house.build.store import (
    ImmutableConflict,
    Layout,
    LocalStore,
    R2Store,
    sha256_bytes,
)

CURRENT_KEYS = {"revision", "dataset_schema_version", "api_schema_version", "minimum_revision", "manifest",
                "manifest_sha256", "published_at", "checked_at", "expires_at", "sig"}


def rename(uid, name):
    return lambda w: w.update_equipment(uid, name=name)


def make(tmp_path, *, flaky=True, window=3, store=None) -> World:
    if store is None:
        inner = LocalStore(tmp_path / "store")
        store = FlakyStore(inner) if flaky else inner
    return World(tmp_path, store=store, window=window)


def current(w: World) -> dict:
    return json.loads(w.store.get(Layout.CURRENT))


def history_of(w: World, key: str, tmp_path: Path) -> Path:
    dest = tmp_path / "dl" / key.replace("/", "_")
    w.store.get_file(key, dest)
    return dest


def pub_rank(key: str) -> int:
    for prefix, rank in (("pub/icons/packs/", 1), ("pub/icons/", 0), ("pub/snapshots/", 2),
                         ("pub/updates/", 3), ("pub/lineage/", 4), ("pub/manifests/", 5)):
        if key.startswith(prefix):
            return rank
    raise AssertionError(key)


def fail_nth(op, matches, n):
    seen = {"n": 0}

    def fail(o, key, index):
        if o == op and matches(key):
            seen["n"] += 1
            return seen["n"] == n
        return False
    return fail


# happy path
def test_two_releases_publish_every_artifact_and_a_verifying_current(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    w.cycle(rename("1001", "B1"))
    manifest = json.loads(w.store.get("pub/manifests/2.json"))
    refs = [manifest["artifacts"]["snapshot"], manifest["artifacts"]["lineage"], *manifest["artifacts"]["updates"],
            manifest["artifacts"]["icon_packs"]["base"], *manifest["artifacts"]["icon_packs"]["deltas"]]
    assert refs
    for ref in refs:
        head = w.store.head("pub/" + ref["path"])
        assert head is not None and head.sha256 == ref["sha256"] and head.size == ref["bytes"]
    stored = current(w)
    assert sign.verify(stored, w.trusted_keys) == 0
    assert stored["revision"] == 2 and stored["manifest"] == "manifests/2.json"
    assert stored["manifest_sha256"] == sha256_bytes(w.store.get("pub/manifests/2.json"))


def test_current_json_is_the_last_put_and_the_manifest_the_last_immutable_one(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    w.cycle(rename("1001", "B1"))
    w.cycle(lambda w: w.upsert_equipment("1005", name="Echo", icon=F.png("echo")), publish=False)
    w.store.log.clear()
    decision = w.release()
    w.publish(decision)
    puts = w.store.puts()
    assert puts[-1] == Layout.CURRENT and puts.count(Layout.CURRENT) == 1
    pub_puts = [k for k in puts if k.startswith("pub/") and k != Layout.CURRENT]
    ranks = [pub_rank(k) for k in pub_puts]
    assert ranks == sorted(ranks), pub_puts                             # icons, packs, snapshot, packages, lineage, manifest
    assert pub_puts[-1] == "pub/manifests/3.json"
    state = [k for k in puts if k.startswith("build/")]
    assert state, puts
    assert max(puts.index(k) for k in pub_puts) < min(puts.index(k) for k in state)     # build state after pub/
    assert max(puts.index(k) for k in state) < puts.index(Layout.CURRENT)               # and before the commit point


def test_build_state_is_uploaded_dated_and_as_current_pointers(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    w.cycle(rename("1001", "B1"))
    for key in ("build/seed/2.sqlite", "build/seed/current.sqlite", "build/history/2.duckdb",
                "build/history/current.duckdb"):
        assert w.store.head(key) is not None, key
    assert w.store.head("build/seed/current.sqlite").sha256 == w.store.head("build/seed/2.sqlite").sha256
    assert w.store.head("build/history/current.duckdb").sha256 == w.store.head("build/history/2.duckdb").sha256
    assert w.store.get("build/seed/2.sqlite") == (w.work / "seed.sqlite").read_bytes()


def test_history_gets_a_release_row_for_each_published_revision(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    w.cycle(rename("1001", "B1"))
    path = history_of(w, "build/history/current.duckdb", tmp_path)
    rows = F.history_rows(path, "SELECT revision, snapshot_only, gate_level, minimum_revision, manifest_sha256 "
                                "FROM release ORDER BY revision")
    assert [r[:4] for r in rows] == [(1, True, "pass", 1), (2, False, "pass", 1)]
    assert rows[1][4] == current(w)["manifest_sha256"]


def test_publish_result_reports_uploaded_and_reused_objects(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    decision = w.cycle(rename("1001", "B1"), publish=False)
    result = w.publish(decision)
    assert result.revision == 2 and result.current == current(w)
    assert "pub/manifests/2.json" in result.uploaded or "manifests/2.json" in result.uploaded
    assert result.published_at == F.utc_text(w.now)


# current json
def test_current_json_shape_size_and_invariants(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    w.cycle(rename("1001", "B1"))
    raw = w.store.get(Layout.CURRENT)
    doc = json.loads(raw)
    assert len(raw) < 512
    assert set(doc) == CURRENT_KEYS
    manifest = json.loads(w.store.get("pub/manifests/2.json"))
    assert doc["manifest"] == f"manifests/{doc['revision']}.json"
    for key in ("revision", "dataset_schema_version", "api_schema_version", "minimum_revision"):
        assert doc[key] == manifest[key], key
    assert doc["minimum_revision"] <= doc["revision"]
    assert doc["published_at"] == doc["checked_at"] == F.utc_text(w.now)
    assert doc["expires_at"] == F.utc_text(w.now + timedelta(days=14))
    assert raw == json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert re.fullmatch(r"[A-Za-z0-9+/]{86}==", doc["sig"])


def test_current_json_validates_against_the_contract(tmp_path):
    if not F.CONTRACT_DIR.is_dir():
        pytest.skip("T4 contract schemas not present")
    import jsonschema
    w = make(tmp_path)
    w.cycle(base_step)
    schema = json.loads((F.CONTRACT_DIR / "current.schema.json").read_text())
    assert not list(jsonschema.Draft202012Validator(schema).iter_errors(current(w)))


def test_make_current_copies_the_manifest_fields(tmp_path):
    w = make(tmp_path)
    decision = w.cycle(base_step, publish=False)
    manifest = w.manifest(decision)
    doc = publish.make_current(manifest, "a" * 64, published_at="2026-10-05T23:21:04Z",
                               checked_at="2026-10-05T23:21:04Z")
    assert set(doc) == CURRENT_KEYS - {"sig"}
    assert doc["manifest"] == "manifests/1.json" and doc["manifest_sha256"] == "a" * 64
    assert doc["expires_at"] == "2026-10-19T23:21:04Z"
    assert doc["api_schema_version"] == manifest["api_schema_version"]


def f_step1(w):
    w.store.fail_on = fail_nth("put", lambda k: k.startswith("pub/") and k != Layout.CURRENT, 2)
    return {}


def f_step2(w):
    w.store.corrupt_get = {"pub/snapshots/unison-2.sqlite.gz": 1}
    return {}


def f_step3(w):
    return {"trusted_keys": (F.World(w.root / "other").trusted_keys)}


def f_step4(w):
    w.store.corrupt_get = {"pub/updates/1-2.json.gz": 2}
    return {}


def f_step5(w):
    w.store.fail_on = fail_nth("put", lambda k: k == Layout.HISTORY_CURRENT, 1)
    return {}


def f_step6(w):
    w.store.fail_on = fail_nth("put", lambda k: k == Layout.CURRENT, 1)
    return {}


FAILURES = [
    pytest.param(1, f_step1, id="step1-put-fails"),
    pytest.param(2, f_step2, id="step2-corrupt-read-back"),
    pytest.param(3, f_step3, id="step3-untrusted-signer"),
    pytest.param(4, f_step4, id="step4-stored-package-tampered"),
    pytest.param(5, f_step5, id="step5-build-state-put-fails"),
    pytest.param(6, f_step6, id="step6-current-put-fails"),
]


@pytest.mark.parametrize("step, arrange", FAILURES)
def test_a_failure_never_moves_current_json(tmp_path, step, arrange):
    w = make(tmp_path)
    w.cycle(base_step)
    decision = w.cycle(rename("1001", "B1"), publish=False)
    before = w.store.get(Layout.CURRENT)
    kwargs = arrange(w)
    with pytest.raises(PublishError) as exc:
        w.publish(decision, **kwargs)
    assert exc.value.step == step
    assert w.store.get(Layout.CURRENT) == before
    assert current(w)["revision"] == 1


@pytest.mark.parametrize("step, arrange", FAILURES[:4])
def test_a_failure_before_the_build_state_leaves_the_build_state_alone(tmp_path, step, arrange):
    w = make(tmp_path)
    w.cycle(base_step)
    decision = w.cycle(rename("1001", "B1"), publish=False)
    history_before = w.store.head(Layout.HISTORY_CURRENT).sha256
    seed_before = w.store.head(Layout.SEED_CURRENT).sha256
    with pytest.raises(PublishError):
        w.publish(decision, **arrange(w))
    assert w.store.head(Layout.history_at(2)) is None and w.store.head(Layout.seed_at(2)) is None
    assert w.store.head(Layout.HISTORY_CURRENT).sha256 == history_before
    assert w.store.head(Layout.SEED_CURRENT).sha256 == seed_before


@pytest.mark.parametrize("step, arrange", FAILURES[:5])
def test_a_failed_first_publish_leaves_no_current_json(tmp_path, step, arrange):
    w = make(tmp_path)
    decision = w.cycle(base_step, publish=False)
    kwargs = arrange_first(w, step)
    with pytest.raises(PublishError) as exc:
        w.publish(decision, **kwargs)
    assert exc.value.step == step
    assert w.store.head(Layout.CURRENT) is None


def arrange_first(w, step):
    if step == 1:
        w.store.fail_on = fail_nth("put", lambda k: k.startswith("pub/") and k != Layout.CURRENT, 2)
    elif step == 2:
        w.store.corrupt_get = {"pub/snapshots/unison-1.sqlite.gz": 1}
    elif step == 3:
        return {"trusted_keys": F.World(w.root / "other").trusted_keys}
    elif step == 4:
        w.store.corrupt_get = {"pub/snapshots/unison-1.sqlite.gz": 2}
    elif step == 5:
        w.store.fail_on = fail_nth("put", lambda k: k == Layout.HISTORY_CURRENT, 1)
    return {}


def test_retry_after_a_step_1_failure_succeeds_with_reused_objects(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    decision = w.cycle(rename("1001", "B1"), publish=False)
    w.store.fail_on = fail_nth("put", lambda k: k.startswith("pub/") and k != Layout.CURRENT, 3)
    with pytest.raises(PublishError) as exc:
        w.publish(decision)
    assert exc.value.step == 1
    w.store.fail_on = None
    result = w.publish(decision)
    assert result.reused and result.uploaded
    assert current(w)["revision"] == 2


def test_the_signer_must_be_trusted_by_the_app(tmp_path):
    w = make(tmp_path)
    decision = w.cycle(base_step, publish=False)
    other = F.World(tmp_path / "other")
    with pytest.raises(PublishError) as exc:
        w.publish(decision, signer=other.signer, trusted_keys=w.trusted_keys)
    assert exc.value.step == 3 and w.store.head(Layout.CURRENT) is None


def test_untrusted_state_keys_fail_closed_on_an_r2_store(tmp_path, monkeypatch):
    from ul_house.sync import keys
    monkeypatch.setattr(keys, "PUBLIC_KEYS", ())
    w = World(tmp_path, store=R2Store(FakeS3Client(), "ul-house"))
    decision = w.cycle(base_step, publish=False)
    with pytest.raises(PublishError) as exc:
        w.publish(decision, trusted_keys=None)                    # nothing injected, PUBLIC_KEYS empty
    assert exc.value.step == 3
    assert w.store.head(Layout.CURRENT) is None


def test_republishing_a_published_revision_is_a_revision_conflict(tmp_path):
    w = make(tmp_path)
    decision = w.cycle(base_step)
    with pytest.raises(RevisionConflict) as exc:
        w.publish(decision)
    assert exc.value.step == 0


def test_a_bundle_older_than_current_is_a_revision_conflict(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    old = w.cycle(rename("1001", "B1"), publish=False)
    w.publish(old)
    w.cycle(rename("1001", "B2"))
    before = w.store.get(Layout.CURRENT)
    with pytest.raises(RevisionConflict):
        w.publish(old)
    assert w.store.get(Layout.CURRENT) == before


def test_an_existing_object_with_other_bytes_is_an_immutable_conflict(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    decision = w.cycle(rename("1001", "B1"), publish=False)
    before = w.store.get(Layout.CURRENT)
    w.store.put("pub/updates/1-2.json.gz", b"squatter")
    with pytest.raises(ImmutableConflict) as exc:
        w.publish(decision)
    assert isinstance(exc.value, PublishError) and exc.value.step == 1
    assert w.store.get("pub/updates/1-2.json.gz") == b"squatter"
    assert w.store.get(Layout.CURRENT) == before


def test_a_tampered_bundle_file_is_refused_at_step_0(tmp_path):
    w = make(tmp_path)
    decision = w.cycle(base_step, publish=False)
    bundle = w.bundle(decision)                                   # loaded (and hashed) while still intact
    target = decision.bundle_dir / "pub" / "snapshots" / "unison-1.sqlite.gz"
    target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(PublishError) as exc:
        publish.publish(bundle, w.store, signer=w.signer, trusted_keys=w.trusted_keys, clock=w.clock,
                        contract_dir=w.contract_dir, work_dir=w.work / "publish")
    assert exc.value.step == 0 and w.store.head(Layout.CURRENT) is None
    assert w.store.puts() == []


# dirty builds
def test_a_dirty_bundle_never_goes_to_an_r2_kind_store(tmp_path):
    w = World(tmp_path, store=R2Store(FakeS3Client(), "ul-house"))
    base_step(w)
    w.commit()
    decision = w.release(build=w.build_info(dirty=True))
    with pytest.raises(DirtyBuild) as exc:
        w.publish(decision, allow_dirty=True)
    assert exc.value.step == 0


def test_a_dirty_bundle_needs_allow_dirty_even_locally(tmp_path):
    w = make(tmp_path)
    base_step(w)
    w.commit()
    decision = w.release(build=w.build_info(dirty=True))
    with pytest.raises(DirtyBuild):
        w.publish(decision)
    assert w.store.head(Layout.CURRENT) is None
    w.publish(decision, allow_dirty=True)
    assert current(w)["revision"] == 1


# candidates
def held(w: World, name="B1", uid="1001"):
    w.gate("blocker", [{"check": "unexplained_churn", "scope": "catalog", "level": "blocker", "count": 4,
                        "pct": 40.0, "message": "churn"}])
    w.update_equipment(uid, name=name)
    w.commit()
    decision = w.release()
    assert decision.action == "hold"
    return decision


def snapshot_of_keys(store, *prefixes):
    return {i.key: i.sha256 for p in prefixes for i in store.list(p)}


def test_hold_writes_the_candidate_and_touches_nothing_else(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    decision = held(w)
    before = snapshot_of_keys(w.store, "pub/", "build/")
    prefix = publish.hold(w.bundle(decision), w.store)
    assert prefix == "candidates/2/"
    assert snapshot_of_keys(w.store, "pub/", "build/") == before
    bundle = w.bundle(decision)
    stored = {i.key for i in w.store.list("candidates/2/")}
    assert stored == {"candidates/2/" + f.path for f in bundle.files} | {"candidates/2/bundle.json"}
    assert "candidates/2/build/seed.sqlite" in stored and "candidates/2/build/history.duckdb" in stored
    for f in bundle.files:
        assert w.store.head("candidates/2/" + f.path).sha256 == f.sha256
    assert w.store.puts()[-1] == "candidates/2/bundle.json"                    # bundle.json last


def test_holding_again_replaces_the_candidate_and_older_ones(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    publish.hold(w.bundle(held(w)), w.store)
    w.store.put("candidates/2/stale-file", b"x")
    w.store.put("candidates/1/leftover", b"x")
    publish.hold(w.bundle(w.release()), w.store)                               # same revision, rebuilt
    keys = {i.key for i in w.store.list("candidates/")}
    assert "candidates/2/stale-file" not in keys and "candidates/1/leftover" not in keys
    assert "candidates/2/bundle.json" in keys


def test_a_newer_candidate_supersedes_an_unapproved_one(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    publish.hold(w.bundle(held(w)), w.store)
    w.update_equipment("1001", name="B2")
    w.commit()
    publish.hold(w.bundle(w.release()), w.store)
    keys = {i.key.split("/")[1] for i in w.store.list("candidates/")}
    assert keys == {"3"}


def approve_kwargs(w, tmp_path):
    return {"reason": "reviewed the churn", "approver": "octocat", "run_id": "approve-run-9",
                "work_dir": tmp_path / "approve", "signer": w.signer, "trusted_keys": w.trusted_keys, "clock": w.clock,
                "contract_dir": w.contract_dir}


def test_approve_publishes_exactly_the_candidates_bytes(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    decision = held(w)
    bundle = w.bundle(decision)
    publish.hold(bundle, w.store)
    result = publish.approve(2, w.store, **approve_kwargs(w, tmp_path))
    assert result.revision == 2 and current(w)["revision"] == 2
    for f in bundle.pub_files():
        assert w.store.head(f.path).sha256 == f.sha256, f.path                   # the candidate's bytes
    assert sign.verify(current(w), w.trusted_keys) == 0
    assert not list(w.store.list("candidates/2/"))


def test_approve_records_the_approval_in_the_promoted_history(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    publish.hold(w.bundle(held(w)), w.store)
    publish.approve(2, w.store, **approve_kwargs(w, tmp_path))
    path = history_of(w, Layout.HISTORY_CURRENT, tmp_path)
    (row,) = F.history_rows(path, "SELECT revision, candidate_run_id, approved_by, reason, approval_run_id, "
                                  "approved_at, observed_at FROM obs_approval")
    assert row[:5] == (2, "run-1", "octocat", "reviewed the churn", "approve-run-9")
    assert row[5] == row[6] == F.utc_text(w.now)
    (rel,) = F.history_rows(path, "SELECT gate_level FROM release WHERE revision = 2")
    assert rel == ("blocker",)


def test_approving_twice_or_after_current_moved_on_is_stale(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    publish.hold(w.bundle(held(w)), w.store)
    publish.approve(2, w.store, **approve_kwargs(w, tmp_path))
    with pytest.raises(CandidateStale):
        publish.approve(2, w.store, **approve_kwargs(w, tmp_path))


def test_a_candidate_whose_revision_the_store_has_passed_is_stale(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    publish.hold(w.bundle(held(w)), w.store)
    w.store.put("pub/manifests/5.json", b"{}")                                  # the store moved on
    with pytest.raises(CandidateStale):
        publish.approve(2, w.store, **approve_kwargs(w, tmp_path))
    assert current(w)["revision"] == 1


def test_approving_a_missing_candidate(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    with pytest.raises(CandidateMissing):
        publish.approve(99, w.store, **approve_kwargs(w, tmp_path))


def test_a_normal_publish_of_n_deletes_the_candidate_of_n(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    publish.hold(w.bundle(held(w)), w.store)
    w.gate("pass")
    decision = w.release()                                                      # the next run, no blocker
    assert decision.action == "publish"
    w.publish(decision)
    assert not w.store.list("candidates/")
    assert current(w)["revision"] == 2


def test_fetch_candidate_verifies_every_file(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    publish.hold(w.bundle(held(w)), w.store)
    bundle = publish.fetch_candidate(2, w.store, tmp_path / "fetched")
    assert isinstance(bundle, Bundle) and bundle.revision == 2
    w.store.put("candidates/2/pub/updates/1-2.json.gz", b"corrupted")
    with pytest.raises(Exception):
        publish.fetch_candidate(2, w.store, tmp_path / "fetched2")


def test_heartbeat_refreshes_times_and_resigns(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    w.cycle(rename("1001", "B1"))
    before = current(w)
    w.now += timedelta(days=1)
    w.store.log.clear()
    fresh = publish.heartbeat(w.store, signer=w.signer, trusted_keys=w.trusted_keys, clock=w.clock,
                              build_state=BuildState(seed=w.work / "seed.sqlite",
                                                     history=w.work / "history" / "current.duckdb"))
    stored = current(w)
    assert stored == fresh and set(stored) == CURRENT_KEYS
    for key in ("revision", "manifest_sha256", "published_at", "api_schema_version", "dataset_schema_version",
                "minimum_revision", "manifest"):
        assert stored[key] == before[key], key
    assert stored["checked_at"] == F.utc_text(w.now) != before["checked_at"]
    assert stored["expires_at"] == F.utc_text(w.now + timedelta(days=14))
    assert sign.verify(stored, w.trusted_keys) == 0 and stored["sig"] != before["sig"]
    assert w.store.get(Layout.CURRENT) == publish.current_bytes(stored)


def test_heartbeat_uploads_only_the_current_pointers_and_current_json_last(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    w.now += timedelta(days=1)
    w.store.log.clear()
    publish.heartbeat(w.store, signer=w.signer, trusted_keys=w.trusted_keys, clock=w.clock,
                      build_state=BuildState(seed=w.work / "seed.sqlite",
                                             history=w.work / "history" / "current.duckdb"))
    assert w.store.puts() == [Layout.SEED_CURRENT, Layout.HISTORY_CURRENT, Layout.CURRENT]


def test_heartbeat_without_build_state_uploads_only_current_json(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    w.store.log.clear()
    publish.heartbeat(w.store, signer=w.signer, trusted_keys=w.trusted_keys, clock=w.clock, build_state=None)
    assert w.store.puts() == [Layout.CURRENT]


def test_heartbeat_needs_a_current_json(tmp_path):
    w = make(tmp_path)
    with pytest.raises(PublishError):
        publish.heartbeat(w.store, signer=w.signer, trusted_keys=w.trusted_keys, clock=w.clock, build_state=None)


def test_heartbeat_refuses_a_current_the_app_would_not_trust(tmp_path):
    w = make(tmp_path)
    w.cycle(base_step)
    before = w.store.get(Layout.CURRENT)
    other = F.World(tmp_path / "other")
    with pytest.raises(Exception):
        publish.heartbeat(w.store, signer=w.signer, trusted_keys=other.trusted_keys, clock=w.clock,
                          build_state=None)
    assert w.store.get(Layout.CURRENT) == before


# post-publish check
API = "https://api.example.test"


def api_fetch(store, *, serve=None):
    """GET served from store files"""
    def fetch(url):
        assert url.startswith(API + "/v1/"), url
        path = url[len(API):]
        n = json.loads(store.get(Layout.CURRENT))["revision"]
        if path == "/v1/version":
            key = Layout.CURRENT
        elif path.startswith("/v1/manifests/"):
            key = f"pub/manifests/{path.rsplit('/', 1)[1]}.json"
        elif path.startswith("/v1/snapshots/"):
            key = f"pub/snapshots/unison-{path.rsplit('/', 1)[1]}.sqlite.gz"
        elif path.startswith("/v1/updates/"):
            key = f"pub/updates/{path.rsplit('/', 1)[1]}-{n}.json.gz"
        elif path.startswith("/v1/icons/packs/"):
            key = "pub/icons/packs/" + path.rsplit("/", 1)[1]
        else:
            raise AssertionError(path)
        body = store.get(key)
        return serve(path, body) if serve else body
    return fetch


def published_world(tmp_path):
    w = make(tmp_path, flaky=False)
    w.cycle(base_step)
    w.cycle(lambda w: w.upsert_equipment("1005", name="Echo", icon=F.png("echo")))     # packages and a delta pack
    return w


def run_check(w, fetch, **kw):
    sleeps = []
    kw.setdefault("attempts", 4)
    report = publish.post_publish_check(API, trusted_keys=w.trusted_keys, expected_revision=2, fetch=fetch,
                                        delay=0.0, sleep=sleeps.append, **kw)
    return report, sleeps


def test_post_publish_check_passes_and_asks_for_every_artifact(tmp_path):
    w = published_world(tmp_path)
    asked = []

    def fetch(url):
        asked.append(url[len(API):])
        return api_fetch(w.store)(url)
    report, sleeps = run_check(w, fetch)
    assert report.ok and sleeps == []
    assert asked == ["/v1/version", "/v1/manifests/2", "/v1/snapshots/2", "/v1/updates/1",
                     "/v1/icons/packs/delta-1-2.zip"]


def test_post_publish_check_without_packages_asks_only_what_exists(tmp_path):
    w = make(tmp_path, flaky=False)
    w.cycle(base_step)
    asked = []

    def fetch(url):
        asked.append(url[len(API):])
        return api_fetch(w.store)(url)
    publish.post_publish_check(API, trusted_keys=w.trusted_keys, expected_revision=1, fetch=fetch,
                               delay=0.0, sleep=lambda s: None)
    assert asked == ["/v1/version", "/v1/manifests/1", "/v1/snapshots/1"]


def test_wrong_revision_on_every_attempt_fails(tmp_path):
    w = published_world(tmp_path)
    stale = json.dumps(w.signer.sign({k: v for k, v in current(w).items() if k != "sig"} | {"revision": 1,
                       "manifest": "manifests/1.json", "minimum_revision": 1}),
                       sort_keys=True, separators=(",", ":")).encode()
    fetch = api_fetch(w.store, serve=lambda path, body: stale if path == "/v1/version" else body)
    sleeps = []
    with pytest.raises(PostPublishCheckFailed):
        publish.post_publish_check(API, trusted_keys=w.trusted_keys, expected_revision=2, fetch=fetch,
                                   attempts=4, delay=0.0, sleep=sleeps.append)
    assert len(sleeps) == 3                                                       # 4 attempts, waits between them


def test_a_manifest_with_one_byte_changed_fails(tmp_path):
    w = published_world(tmp_path)
    fetch = api_fetch(w.store, serve=lambda path, body: body[:-1] + b" " if path.startswith("/v1/manifests/") else body)
    with pytest.raises(PostPublishCheckFailed):
        run_check(w, fetch)


def test_a_snapshot_with_one_byte_changed_fails(tmp_path):
    w = published_world(tmp_path)
    fetch = api_fetch(w.store, serve=lambda path, body: body + b"x" if path.startswith("/v1/snapshots/") else body)
    with pytest.raises(PostPublishCheckFailed):
        run_check(w, fetch)


def test_a_changed_package_or_delta_pack_fails(tmp_path):
    w = published_world(tmp_path)
    for prefix in ("/v1/updates/", "/v1/icons/packs/"):
        fetch = api_fetch(w.store, serve=lambda path, body, p=prefix: body + b"x" if path.startswith(p) else body)
        with pytest.raises(PostPublishCheckFailed):
            run_check(w, fetch)


def test_a_bad_signature_fails(tmp_path):
    w = published_world(tmp_path)

    def tamper(path, body):
        if path != "/v1/version":
            return body
        doc = json.loads(body)
        doc["expires_at"] = "2030-01-01T00:00:00Z"                                  # signed field changed
        return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(PostPublishCheckFailed):
        run_check(w, api_fetch(w.store, serve=tamper))


def test_an_untrusted_signer_fails_the_check(tmp_path):
    w = published_world(tmp_path)
    other = F.World(tmp_path / "other")
    with pytest.raises(PostPublishCheckFailed):
        publish.post_publish_check(API, trusted_keys=other.trusted_keys, expected_revision=2,
                                   fetch=api_fetch(w.store), attempts=2, delay=0.0, sleep=lambda s: None)


def test_the_check_passes_when_the_revision_is_right_only_from_the_third_attempt(tmp_path):
    w = published_world(tmp_path)
    old = w.signer.sign({**{k: v for k, v in current(w).items() if k != "sig"}, "revision": 1,
                         "manifest": "manifests/1.json", "minimum_revision": 1})
    stale = json.dumps(old, sort_keys=True, separators=(",", ":")).encode()
    calls = {"version": 0}

    def serve(path, body):
        if path == "/v1/version":
            calls["version"] += 1
            return stale if calls["version"] < 3 else body
        return body
    report, sleeps = run_check(w, api_fetch(w.store, serve=serve), attempts=5)
    assert report.ok and len(sleeps) == 2 and calls["version"] == 3


def test_a_stale_update_response_is_retried_until_it_matches(tmp_path):
    w = published_world(tmp_path)
    calls = {"updates": 0}

    def serve(path, body):
        if path.startswith("/v1/updates/"):
            calls["updates"] += 1
            return b"404 no_update" if calls["updates"] == 1 else body
        return body
    report, sleeps = run_check(w, api_fetch(w.store, serve=serve), attempts=4)
    assert report.ok and len(sleeps) == 1


def test_the_default_retry_budget_outlasts_the_300_second_cache():
    import inspect
    sig = inspect.signature(publish.post_publish_check)
    assert sig.parameters["attempts"].default * sig.parameters["delay"].default > 300
    assert sig.parameters["attempts"].default == 13 and sig.parameters["delay"].default == 30.0


def test_urllib_fetch_refuses_plain_http_to_remote_hosts_before_any_request():
    with pytest.raises(PublishError):
        publish.urllib_fetch("http://example.invalid/v1/version")


def retention_world(tmp_path, *, weekly: bool = False) -> World:
    w = make(tmp_path, flaky=False, window=3)
    icon_x = F.png("x")
    steps = [
        base_step,
        rename("1001", "B1"),
        lambda w: w.upsert_equipment("1009", name="Ico", icon=icon_x),
        rename("1001", "B2"),
        lambda w: (w.update_equipment("1009", icon_sha=None), w.delete_shared("icon", F.sha256(icon_x)))[0],
        rename("1001", "B3"),
        rename("1001", "B4"),
        rename("1001", "B5"),
    ]
    for step in steps:
        w.cycle(step)
        if weekly:
            w.now += timedelta(days=7)
    return w


def listing(w):
    return {i.key for i in w.store.list("")}


def test_retention_by_minimum_revision(tmp_path):
    w = retention_world(tmp_path)
    assert current(w)["revision"] == 8 and current(w)["minimum_revision"] == 5
    icon_key = "pub/icons/equipment/" + F.sha256(F.png("x")) + ".png"
    assert icon_key in listing(w)
    report = publish.retain(w.store, clock=w.clock)
    keys = listing(w)
    for r in (1, 2, 3, 4):                                       # below minimum_revision, same ISO week: gone
        assert f"pub/manifests/{r}.json" not in keys and f"pub/lineage/{r}.json.gz" not in keys
        assert f"pub/snapshots/unison-{r}.sqlite.gz" not in keys and f"pub/icons/packs/base-{r}.zip" not in keys
        assert Layout.seed_at(r) not in keys and Layout.history_at(r) not in keys
    for r in (5, 6, 7, 8):
        assert f"pub/manifests/{r}.json" in keys and f"pub/lineage/{r}.json.gz" in keys
        assert f"pub/snapshots/unison-{r}.sqlite.gz" in keys and f"pub/icons/packs/base-{r}.zip" in keys
        assert Layout.seed_at(r) in keys and Layout.history_at(r) in keys
    for key in keys:
        parsed = Layout.parse_update_key(key) or Layout.parse_delta_pack_key(key)
        if parsed:
            assert parsed[1] >= 5, key
    assert icon_key not in keys                                    # no retained lineage references it
    assert Layout.CURRENT in keys and Layout.SEED_CURRENT in keys and Layout.HISTORY_CURRENT in keys
    assert report.deleted and not report.dry_run
    assert set(report.deleted).isdisjoint(keys)


def test_retention_keeps_every_object_of_the_current_release(tmp_path):
    w = retention_world(tmp_path)
    publish.retain(w.store, clock=w.clock)
    manifest = json.loads(w.store.get("pub/manifests/8.json"))
    a = manifest["artifacts"]
    refs = [a["snapshot"], a["lineage"], *a["updates"], a["icon_packs"]["base"], *a["icon_packs"]["deltas"]]
    for ref in refs:
        assert w.store.head("pub/" + ref["path"]) is not None, ref["path"]
    lineage = F.read_json_gz(w.store.get("pub/lineage/8.json.gz"))
    for icon in lineage["icons"]:
        assert w.store.head("pub/" + Layout.rel_icon(icon["kind"], icon["sha256"])) is not None


def test_retention_dry_run_deletes_nothing(tmp_path):
    w = retention_world(tmp_path)
    before = snapshot_of_keys(w.store, "")
    report = publish.retain(w.store, clock=w.clock, dry_run=True)
    assert report.dry_run and report.deleted
    assert snapshot_of_keys(w.store, "") == before
    again = publish.retain(w.store, clock=w.clock)
    assert again.deleted == report.deleted


def test_retention_never_deletes_what_it_does_not_recognise(tmp_path):
    w = retention_world(tmp_path)
    for key in ("pub/weird.txt", "pub/manifests/notes.json", "pub/updates/readme", "build/other/file",
                "something/else"):
        w.store.put(key, b"?")
    report = publish.retain(w.store, clock=w.clock)
    keys = listing(w)
    for key in ("pub/weird.txt", "pub/manifests/notes.json", "pub/updates/readme", "build/other/file",
                "something/else"):
        assert key in keys and key in report.unknown, key


def test_retention_of_archives_checkpoint_backups_and_candidates(tmp_path):
    w = retention_world(tmp_path)
    old = F.utc_text(w.now - timedelta(days=20))
    new = F.utc_text(w.now - timedelta(days=2))
    w.store.put("build/history/archived/2026-Q3.duckdb", b"arc")
    w.store.put(Layout.CHECKPOINT, b"{}")
    w.store.put("build/history/backups/pre-20260901T030000Z.duckdb", b"old")          # > 14 days old
    w.store.put("build/history/backups/pre-20261003T030000Z.duckdb", b"new")
    for r, created in ((3, new), (20, old), (21, new)):
        w.store.put(f"candidates/{r}/bundle.json", json.dumps({"created_at": created}).encode())
        w.store.put(f"candidates/{r}/pub/manifests/{r}.json", b"{}")
    publish.retain(w.store, clock=w.clock)
    keys = listing(w)
    assert "build/history/archived/2026-Q3.duckdb" in keys and Layout.CHECKPOINT in keys
    assert "build/history/backups/pre-20260901T030000Z.duckdb" not in keys
    assert "build/history/backups/pre-20261003T030000Z.duckdb" in keys
    assert not [k for k in keys if k.startswith("candidates/3/")]                       # r <= current: obsolete
    assert not [k for k in keys if k.startswith("candidates/20/")]                      # older than 14 days
    assert [k for k in keys if k.startswith("candidates/21/")]                          # young and ahead of current


def test_retention_keeps_one_snapshot_per_week_within_three_months(tmp_path):
    w = retention_world(tmp_path, weekly=True)                  # one release a week
    icon_key = "pub/icons/equipment/" + F.sha256(F.png("x")) + ".png"
    w.now += timedelta(days=7 * 7 + 3)                          # retention runs 7 weeks later
    publish.retain(w.store, clock=w.clock)
    keys = listing(w)
    snapshots = {Layout.parse_snapshot_key(k) for k in keys if Layout.parse_snapshot_key(k)}
    assert snapshots == {3, 4, 5, 6, 7, 8}                      # revisions 1 and 2 are expired
    for r in snapshots:
        assert f"pub/lineage/{r}.json.gz" in keys and Layout.seed_at(r) in keys and Layout.history_at(r) in keys
    assert icon_key in keys                                      # retained lineage still lists icon
    for r in (1, 2):
        assert f"pub/manifests/{r}.json" not in keys


def test_retention_with_all_snapshots_inside_one_week_keeps_only_the_newest(tmp_path):
    w = retention_world(tmp_path)
    publish.retain(w.store, clock=w.clock)
    snapshots = {Layout.parse_snapshot_key(k) for k in listing(w) if Layout.parse_snapshot_key(k)}
    assert snapshots == {5, 6, 7, 8}


def test_retention_without_a_current_json_does_nothing(tmp_path):
    w = make(tmp_path, flaky=False)
    w.store.put("pub/manifests/1.json", b"{}")
    report = publish.retain(w.store, clock=w.clock)
    assert report.deleted == () and w.store.head("pub/manifests/1.json") is not None


def test_canonical_bytes_is_importable_from_release_for_bundles():
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
