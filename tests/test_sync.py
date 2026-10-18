"""The driver: cycle, schedule, worker thread."""
import json
import queue
import random
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import pub_fixture as pf
from local_server import LocalServer, Reply
from ul_house import database
from ul_house.http.client import RETRY_STATUSES, Client
from ul_house.sync import icons, service, verify
from ul_house.sync.service import Outcome, Schedule, SyncConfig, SyncResult

W1 = pf.icon_sha("w1")


@pytest.fixture
def server():
    with LocalServer() as running:
        yield running


@pytest.fixture
def db(tmp_path):
    opened = []

    def make(revision, name=None, **kw):
        conn = pf.client_db_at(tmp_path / (name or f"c{len(opened)}.sqlite"), revision, **kw)
        opened.append(conn)
        return conn

    yield make
    for conn in opened:
        conn.close()


@pytest.fixture
def pub(tmp_path, server):
    published = pf.build_pub(tmp_path / "p")
    pf.serve(published, server)
    return published


def paths(server):
    return [h.path for h in server.log]


def sync(conn, server, pub, tmp_path, **kw):
    client = kw.pop("client", None) or pf.make_client(server)
    return pf.run_sync(conn, client, pub, tmp_path, **kw)


def state_of(conn):
    return pf.everything(conn, rowid=True)


def test_up_to_date_costs_one_request_and_remembers_the_etag(db, server, pub, tmp_path):
    conn = db(4)
    result = sync(conn, server, pub, tmp_path)
    assert result == SyncResult(Outcome.UP_TO_DATE, 4, 4, False, None, 0, result.icons_missing)
    assert paths(server) == ["/v1/version"]
    etag = database.get_state(conn, "current_etag")
    assert etag and etag.startswith('"')
    assert database.get_state(conn, "last_checked_at") == "2026-10-05T12:00:00Z"
    assert database.get_state(conn, "expires_at") == pf.EXPIRES_AT
    assert database.get_state(conn, "revision") is None

    again = sync(conn, server, pub, tmp_path)
    assert again.outcome is Outcome.UP_TO_DATE and again.remote_revision == 4
    assert paths(server) == ["/v1/version", "/v1/version"]
    assert server.log[1].headers.get("If-None-Match") == etag
    assert server.log[0].headers.get("If-None-Match") is None


def test_a_new_release_changes_the_etag_and_is_not_a_304(db, server, pub, tmp_path):
    conn = db(1)
    database.set_state(conn, {"current_etag": '"older-release"'})
    result = sync(conn, server, pub, tmp_path)
    assert result.outcome is Outcome.UPDATED
    assert server.log[0].headers.get("If-None-Match") == '"older-release"'
    assert database.get_state(conn, "current_etag") not in (None, '"older-release"')


def test_a_fresh_install_never_sends_a_stored_etag(db, server, pub, tmp_path):
    conn = db(None)
    database.set_state(conn, {"current_etag": '"leftover"'})
    sync(conn, server, pub, tmp_path)
    assert server.log[0].headers.get("If-None-Match") is None


def test_the_update_path_makes_exactly_four_requests(db, server, pub, tmp_path):
    conn = db(1)
    cache = icons.IconCache(tmp_path / "icons")
    result = sync(conn, server, pub, tmp_path, cache=cache)
    assert paths(server) == ["/v1/version", "/v1/manifests/4", "/v1/updates/1", "/v1/icons/packs/delta-1-4.zip"]
    assert result.outcome is Outcome.UPDATED
    assert (result.local_revision, result.remote_revision, result.stale) == (4, 4, False)
    assert result.icons_installed == 2
    manifest = pub.manifest()
    assert conn.execute("SELECT revision, from_revision, kind, applied_at, app_version, package_sha256 FROM sync_history "
                        "WHERE revision = 4").fetchone() == (4, 1, "update", "2026-10-05T12:00:00Z", "test",
                                                             manifest["artifacts"]["updates"][0]["sha256"])
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4)
    assert all(cache.has("equipment", s) for s in pf.added_icons(1, 4))
    assert conn.execute("SELECT count(*) FROM inventory WHERE uid = ?", (pf.Uid.G2,)).fetchone()[0] == 3
    assert database.get_state(conn, "current_etag")


def test_two_cycles_in_a_row(db, server, pub, tmp_path):
    conn = db(2)
    assert sync(conn, server, pub, tmp_path).outcome is Outcome.UPDATED
    assert sync(conn, server, pub, tmp_path).outcome is Outcome.UP_TO_DATE
    assert paths(server).count("/v1/updates/2") == 1


def test_a_current_with_an_unknown_extra_field_is_accepted(db, server, tmp_path):
    published = pf.build_pub(tmp_path / "p", current_hook=lambda c: {**c, "freeze_notice": "later"}, validate=False)
    pf.serve(published, server)
    assert sync(db(1), server, published, tmp_path).outcome is Outcome.UPDATED


def test_a_snapshot_only_release_takes_the_snapshot_path(db, server, tmp_path):
    published = pf.build_pub(tmp_path / "p", snapshot_only=True)
    pf.serve(published, server)
    result = sync(db(3), server, published, tmp_path)
    assert result.outcome is Outcome.SNAPSHOT
    assert paths(server)[:3] == ["/v1/version", "/v1/manifests/4", "/v1/snapshots/4"]
    assert not [p for p in paths(server) if p.startswith("/v1/updates")]


def test_a_bad_signature_is_refused_and_nothing_else_is_fetched(db, server, tmp_path):
    published = pf.build_pub(tmp_path / "p", key=pf.make_keypair("stranger"))
    pf.serve(published, server)
    conn = db(1)
    before = state_of(conn)
    verified = verify.load_public_keys([pf.KEY.public_hex])
    result = sync(conn, server, published, tmp_path, keys=verified)
    assert (result.outcome, result.reason) == (Outcome.REFUSED, "bad_signature")
    assert paths(server) == ["/v1/version"]
    assert state_of(conn) == before
    assert database.get_state(conn, "current_etag") is None


def test_with_no_trusted_key_every_release_is_refused(db, server, pub, tmp_path):
    conn = db(1)
    result = sync(conn, server, pub, tmp_path, keys=verify.load_public_keys(()))
    assert (result.outcome, result.reason) == (Outcome.REFUSED, "bad_signature")
    assert paths(server) == ["/v1/version"]


def test_either_embedded_key_is_trusted(db, server, pub, tmp_path):
    keys = verify.load_public_keys([pf.KEY2.public_hex, pf.KEY.public_hex])
    assert sync(db(1), server, pub, tmp_path, keys=keys).outcome is Outcome.UPDATED


def test_a_lower_revision_is_a_replay_and_is_refused(db, server, tmp_path):
    older = pf.build_pub(tmp_path / "p", revision=3)
    pf.serve(older, server)
    conn = db(4)
    before = state_of(conn)
    result = sync(conn, server, older, tmp_path)
    assert (result.outcome, result.reason) == (Outcome.REFUSED, "revision_lower")
    assert paths(server) == ["/v1/version"] and state_of(conn) == before


@pytest.mark.parametrize("changes,reason", [({"dataset_schema_version": 2}, "schema_unsupported"),
                                            ({"api_schema_version": 2}, "api_unsupported")])
def test_unsupported_versions_are_refused(db, server, tmp_path, changes, reason):
    published = pf.build_pub(tmp_path / "p", **changes)
    pf.serve(published, server)
    conn = db(1)
    before = state_of(conn)
    result = sync(conn, server, published, tmp_path)
    assert (result.outcome, result.reason) == (Outcome.REFUSED, reason)
    assert paths(server) == ["/v1/version"] and state_of(conn) == before


def test_a_manifest_that_is_not_the_signed_one_is_refused(db, server, pub, tmp_path):
    original = pub.read("manifests/4.json")
    pub.write("manifests/4.json", original + b" ")
    conn = db(1)
    before = state_of(conn)
    result = sync(conn, server, pub, tmp_path)
    assert result.outcome is Outcome.FAILED and result.reason
    assert paths(server) == ["/v1/version", "/v1/manifests/4"] and state_of(conn) == before
    assert database.get_state(conn, "current_etag") is None


def test_a_manifest_that_contradicts_the_signed_pointer_is_refused(db, server, tmp_path):
    published = pf.build_pub(tmp_path / "p", manifest_hook=lambda m: {**m, "minimum_revision": 2}, validate=False)
    pf.serve(published, server)
    conn = db(1)
    before = state_of(conn)
    result = sync(conn, server, published, tmp_path)
    assert result.outcome is Outcome.FAILED
    assert paths(server) == ["/v1/version", "/v1/manifests/4"] and state_of(conn) == before


def add_bad_op(r, package):
    package["ops"].append({"op": "UPSERT", "entity": "item", "key": "7002", "rev": package["to_revision"],
                           "data": {"item": {"uid": "7002", "name": None}}})
    return package


def test_an_apply_failure_falls_back_to_the_snapshot_in_the_same_cycle(db, server, tmp_path):
    published = pf.build_pub(tmp_path / "p", package_hook=add_bad_op, validate=False)
    pf.serve(published, server)
    conn = db(1)
    result = sync(conn, server, published, tmp_path)
    assert result.outcome is Outcome.SNAPSHOT
    assert paths(server) == ["/v1/version", "/v1/manifests/4", "/v1/updates/1", "/v1/snapshots/4"]
    assert conn.execute("SELECT revision, from_revision, kind FROM sync_history WHERE revision = 4").fetchone() == (4, 1, "snapshot")
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4)
    assert conn.execute("SELECT count(*) FROM item WHERE uid = '7002'").fetchone()[0] == 0
    assert database.get_state(conn, "current_etag")


def test_the_etag_is_stored_only_after_the_apply_committed(db, server, tmp_path):
    published = pf.build_pub(tmp_path / "p", package_hook=add_bad_op, validate=False)
    pf.serve(published, server)
    server.route("/v1/snapshots/4", Reply(200, b"corrupt", {"Content-Type": "application/gzip", "X-API-Commit": "dev"}))
    conn = db(1)
    database.set_state(conn, {"current_etag": '"old"'})
    before = state_of(conn)
    result = sync(conn, server, published, tmp_path)
    assert result.outcome is Outcome.FAILED
    assert state_of(conn) == before
    assert database.get_state(conn, "current_etag") == '"old"'
    again = sync(conn, server, published, tmp_path)
    assert again.outcome is not Outcome.UP_TO_DATE
    assert all(h.headers.get("If-None-Match") == '"old"' for h in server.hits("/v1/version"))


def test_a_package_that_is_not_the_manifest_one_changes_nothing(db, server, pub, tmp_path):
    good = pub.read("updates/1-4.json.gz")
    pub.write("updates/1-4.json.gz", good[:-1] + bytes([good[-1] ^ 1]))
    conn = db(1)
    before = state_of(conn)
    result = sync(conn, server, pub, tmp_path)
    assert result.outcome is Outcome.FAILED and "manifest" in (result.reason or "")
    assert state_of(conn) == before and database.get_state(conn, "current_etag") is None


def test_a_snapshot_that_is_not_the_manifest_one_changes_nothing(db, server, tmp_path):
    published = pf.build_pub(tmp_path / "p", snapshot_only=True)
    pf.serve(published, server)
    blob = published.read("snapshots/unison-4.sqlite.gz")
    published.write("snapshots/unison-4.sqlite.gz", blob + b"\x00")
    conn = db(1)
    before = state_of(conn)
    result = sync(conn, server, published, tmp_path)
    assert result.outcome is Outcome.FAILED
    assert state_of(conn) == before and not list((tmp_path / "tmp").glob("*"))


def moved(pub, header):
    return Reply(200, pub.read("updates/1-4.json.gz"),
                 {"Content-Type": "application/gzip", "X-API-Commit": "dev", "X-UL-To-Revision": header})


def test_a_moved_pointer_restarts_the_cycle_once(db, server, pub, tmp_path):
    server.route("/v1/updates/1", [moved(pub, "5"), moved(pub, "4")])
    conn = db(1)
    result = sync(conn, server, pub, tmp_path)
    assert result.outcome is Outcome.UPDATED
    assert paths(server) == ["/v1/version", "/v1/manifests/4", "/v1/updates/1",
                             "/v1/version", "/v1/manifests/4", "/v1/updates/1", "/v1/icons/packs/delta-1-4.zip"]
    assert pf.dump(conn, pf.GAME_TABLES) == pf.expected_dump(4)


def test_a_second_move_is_a_failure_not_a_loop(db, server, pub, tmp_path):
    server.route("/v1/updates/1", moved(pub, "5"))
    conn = db(1)
    before = state_of(conn)
    result = sync(conn, server, pub, tmp_path)
    assert result.outcome is Outcome.FAILED
    assert paths(server) == ["/v1/version", "/v1/manifests/4", "/v1/updates/1"] * 2
    assert state_of(conn) == before and database.get_state(conn, "current_etag") is None


def test_the_header_is_checked_before_the_package_is_hashed_or_applied(db, server, pub, tmp_path):
    server.route("/v1/updates/1", Reply(200, b"not even gzip", {"Content-Type": "application/gzip", "X-API-Commit": "dev",
                                                                  "X-UL-To-Revision": "9"}))
    conn = db(1)
    before = state_of(conn)
    result = sync(conn, server, pub, tmp_path)
    assert result.outcome is Outcome.FAILED
    assert paths(server) == ["/v1/version", "/v1/manifests/4", "/v1/updates/1"] * 2
    assert state_of(conn) == before


# sample /v1/updates reply
@pytest.mark.parametrize("reply", [
    Reply(200, b"<html>captive portal</html>", {"Content-Type": "text/html", "X-API-Commit": "dev"}),
    Reply(200, b"x", {"X-API-Commit": "dev"}),
    Reply(404, b"nothing here", {"Content-Type": "text/html"}),
    Reply(403, b"{}", {"Content-Type": "application/json", "X-API-Commit": "dev"}),
])
def test_unexpected_updates_answers_fail_without_applying(db, server, pub, tmp_path, reply):
    server.route("/v1/updates/1", reply)
    conn = db(1)
    before = state_of(conn)
    assert sync(conn, server, pub, tmp_path).outcome is Outcome.FAILED
    assert state_of(conn) == before


def test_404_no_update_is_a_failure_with_its_code(db, server, pub, tmp_path):
    server.route("/v1/updates/1", lambda request: pf.error_reply(404, "no_update"))
    result = sync(db(1), server, pub, tmp_path)
    assert (result.outcome, result.reason) == (Outcome.FAILED, "no_update")


def test_requires_snapshot_is_recognised_by_content_type(db, server, pub, tmp_path):
    for reason in ("below_minimum", "package_missing"):
        server.log.clear()
        server.route("/v1/updates/1", lambda request, reason=reason: pf.requires_snapshot_reply(4, 1, reason))
        conn = db(1, name=f"rs-{reason}.sqlite")
        assert sync(conn, server, pub, tmp_path).outcome is Outcome.SNAPSHOT, reason
        assert paths(server)[:4] == ["/v1/version", "/v1/manifests/4", "/v1/updates/1", "/v1/snapshots/4"]


def config_for(server, tmp_path, **kw):
    options = dict(api_base_url=server.base_url, db_path=tmp_path / "w.sqlite", icon_dir=tmp_path / "icons",
                   app_version="1.0", user_agent="UL-House-test/1.0", launch_jitter=0.0)
    options.update(kw)
    return SyncConfig(**options)


def test_429_with_the_api_commit_header_is_over_the_api_cap(db, server, pub, tmp_path):
    server.route("/v1/version", Reply(429, b"{}", {"Content-Type": "application/json", "X-API-Commit": "dev", "Retry-After": "1"}))
    client = service.make_client(config_for(server, tmp_path), threading.Event())
    conn = db(1)
    result = sync(conn, server, pub, tmp_path, client=client)
    assert (result.outcome, result.reason) == (Outcome.OVER_CAP, "api")
    assert len(server.hits("/v1/version")) == 1


def test_429_without_the_api_commit_header_is_the_platform(db, server, pub, tmp_path):
    server.route("/v1/version", Reply(429, b"<html>error code: 1027</html>", {"Content-Type": "text/html"}))
    client = service.make_client(config_for(server, tmp_path), threading.Event())
    result = sync(db(1), server, pub, tmp_path, client=client)
    assert (result.outcome, result.reason) == (Outcome.OVER_CAP, "platform")
    assert len(server.hits("/v1/version")) == 1


def test_over_cap_anywhere_in_the_cycle(db, server, pub, tmp_path):
    server.route("/v1/manifests/4", Reply(429, b"", {"X-API-Commit": "dev"}))
    result = sync(db(1), server, pub, tmp_path)
    assert result.outcome is Outcome.OVER_CAP and len(server.hits("/v1/manifests/4")) == 1


def test_503_is_offline_and_changes_nothing(db, server, pub, tmp_path):
    server.route("/v1/version", Reply(503, b'{"error": "unavailable", "message": "x"}',
                                      {"Content-Type": "application/json", "X-API-Commit": "dev", "Retry-After": "300"}))
    conn = db(1)
    before = state_of(conn)
    assert sync(conn, server, pub, tmp_path).outcome is Outcome.OFFLINE
    assert state_of(conn) == before


def test_nobody_home_is_offline(db, pub, tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = Client(f"http://127.0.0.1:{port}", interval=0.0, max_retries=0, timeout=(1.0, 1.0))
    conn = db(1)
    before = state_of(conn)
    result = pf.run_sync(conn, client, pub, tmp_path)
    assert result.outcome is Outcome.OFFLINE and result.local_revision == 1
    assert state_of(conn) == before


def test_a_stop_ends_the_cycle_before_any_request(db, server, pub, tmp_path):
    stop = threading.Event()
    stop.set()
    result = sync(db(1), server, pub, tmp_path, client=pf.make_client(server, stop=stop))
    assert result.outcome is Outcome.STOPPED and server.log == []


def test_a_redirect_to_another_origin_is_not_followed(db, server, pub, tmp_path):
    server.route("/v1/version", Reply(302, b"", {"Location": "http://example.invalid/v1/version"}))
    result = sync(db(1), server, pub, tmp_path)
    assert result.outcome is Outcome.FAILED


def test_the_client_the_service_builds(tmp_path):
    config = config_for(LocalServerStub, tmp_path)
    stop = threading.Event()
    client = service.make_client(config, stop)
    try:
        assert client.base_url == "http://127.0.0.1:1/"
        assert 429 not in client.retry_statuses and client.retry_statuses == RETRY_STATUSES - {429}
        assert 503 in client.retry_statuses
        assert client.max_retries == config.max_retries == 2
        assert client.deadline == config.request_deadline and client.stop is stop
        assert client.session.headers["User-Agent"] == "UL-House-test/1.0"
    finally:
        client.close()


class LocalServerStub:
    base_url = "http://127.0.0.1:1"


# stale release
def test_a_stale_release_is_still_applied_and_flagged(db, server, tmp_path):
    published = pf.build_pub(tmp_path / "p", expires_at="2026-10-01T00:00:00Z")
    pf.serve(published, server)
    conn = db(1)
    result = sync(conn, server, published, tmp_path)
    assert result.outcome is Outcome.UPDATED and result.stale is True
    assert database.applied_revision(conn) == 4
    again = sync(conn, server, published, tmp_path)
    assert again.outcome is Outcome.UP_TO_DATE and again.stale is True
    assert database.get_state(conn, "expires_at") == "2026-10-01T00:00:00Z"


def test_a_fresh_release_is_not_stale(db, server, pub, tmp_path):
    conn = db(1)
    assert sync(conn, server, pub, tmp_path).stale is False
    assert sync(conn, server, pub, tmp_path).stale is False
    later = datetime(2026, 10, 18, 0, 0, 1, tzinfo=timezone.utc)
    result = sync(conn, server, pub, tmp_path, now=lambda: later)
    assert result.outcome is Outcome.UP_TO_DATE and result.stale is True


@pytest.mark.parametrize("reply", [
    Reply(404, b"", {"X-API-Commit": "dev"}),
    Reply(200, b"not a zip", {"Content-Type": "application/zip", "X-API-Commit": "dev"}),
    Reply(500, b"", {}),
])
def test_a_bad_delta_pack_does_not_fail_the_update(db, server, pub, tmp_path, reply):
    server.route("/v1/icons/packs/delta-1-4.zip", reply)
    conn = db(1)
    result = sync(conn, server, pub, tmp_path)
    assert result.outcome is Outcome.UPDATED and result.icons_installed == 0
    assert database.applied_revision(conn) == 4 and database.get_state(conn, "current_etag")


def test_withdrawn_icons_are_deleted_from_the_cache(db, server, tmp_path):
    published = pf.build_pub(tmp_path / "p", withdrawn=[W1])
    pf.serve(published, server)
    cache = icons.IconCache(tmp_path / "icons")
    cache.store("equipment", W1, pf.ICON_BYTES[W1])
    sync(db(1), server, published, tmp_path, cache=cache)
    assert not cache.has("equipment", W1)


NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def result_of(outcome):
    return SyncResult(outcome, 4, 4)


def test_launch_jitter_is_uniform_within_its_bound():
    schedule = Schedule(rng=random.Random(7))
    delays = [schedule.first_delay(30.0) for _ in range(200)]
    assert all(0.0 <= d <= 30.0 for d in delays) and max(delays) > 15 and min(delays) < 15
    assert Schedule(rng=random.Random(7)).first_delay(0.0) == 0.0


@pytest.mark.parametrize("outcome", [Outcome.UP_TO_DATE, Outcome.UPDATED, Outcome.SNAPSHOT, Outcome.REFUSED])
def test_normal_polls_are_six_hours_plus_or_minus_ten_percent(outcome):
    schedule = Schedule(rng=random.Random(3))
    delays = [schedule.next_delay(result_of(outcome), NOW) for _ in range(300)]
    assert all(6 * 3600 * 0.9 <= d <= 6 * 3600 * 1.1 for d in delays)
    assert max(delays) - min(delays) > 3600


def test_failures_back_off_exponentially_up_to_the_interval():
    schedule = Schedule(interval=3600, rng=random.Random(11))
    bases = [300, 600, 1200, 2400, 3600, 3600, 3600]
    for n, base in enumerate(bases, start=1):
        outcome = Outcome.OFFLINE if n % 2 else Outcome.FAILED
        delay = schedule.next_delay(result_of(outcome), NOW)
        assert base * 0.5 <= delay <= base * 1.5, (n, delay)
    assert schedule.next_delay(result_of(Outcome.UP_TO_DATE), NOW) <= 3600 * 1.1
    first = schedule.next_delay(result_of(Outcome.OFFLINE), NOW)
    assert 150 <= first <= 450


def test_over_cap_waits_until_after_midnight_utc():
    for now in (datetime(2026, 10, 5, 22, 30, tzinfo=timezone.utc), datetime(2026, 10, 5, 0, 0, 1, tzinfo=timezone.utc),
                datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)):
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        schedule = Schedule(rng=random.Random(5))
        for _ in range(50):
            delay = schedule.next_delay(result_of(Outcome.OVER_CAP), now)
            assert (midnight - now).total_seconds() < delay <= (midnight - now).total_seconds() + 3600 + 1
            assert now + timedelta(seconds=delay) > midnight


def test_a_non_utc_clock_is_normalised():
    tz = timezone(timedelta(hours=-5))
    now = datetime(2026, 10, 5, 20, 0, tzinfo=tz)
    delay = Schedule(rng=random.Random(1)).next_delay(result_of(Outcome.OVER_CAP), now)
    assert 23 * 3600 < delay <= 23 * 3600 + 3601


# SyncConfig
@pytest.mark.parametrize("url", ["https://api.example.invalid", "https://api.example.invalid/prefix/", "http://127.0.0.1:8787",
                                 "http://localhost:8787", "http://[::1]:8787"])
def test_good_base_urls(tmp_path, url):
    assert config_for(type("S", (), {"base_url": url}), tmp_path).api_base_url == url


@pytest.mark.parametrize("url", ["", "http://api.example.invalid", "http://127.0.0.1.example.invalid", "ftp://x.invalid",
                                 "not a url", "https://", "//api.example.invalid", "file:///etc/passwd", "http://localhost.evil.invalid"])
def test_bad_base_urls_are_rejected(tmp_path, url):
    with pytest.raises(ValueError):
        config_for(type("S", (), {"base_url": url}), tmp_path)


def test_there_is_no_default_api_url(tmp_path):
    with pytest.raises(TypeError):
        SyncConfig(db_path=tmp_path / "x", icon_dir=tmp_path / "i", app_version="1", user_agent="ua")      # type: ignore[call-arg]


def test_other_settings_are_validated(tmp_path):
    for bad in ({"poll_interval": 0}, {"poll_interval": -1}, {"request_deadline": 0}, {"launch_jitter": -1}):
        with pytest.raises(ValueError):
            config_for(LocalServerStub, tmp_path, **bad)
    assert config_for(LocalServerStub, tmp_path, public_keys=()).public_keys == ()


# worker thread
def worker_config(server, pub, tmp_path, **kw):
    return config_for(server, tmp_path, public_keys=(pub.public_hex,), poll_interval=3600.0, **kw)


def next_event(worker, kind, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = worker.events.get(timeout=0.2)
        except queue.Empty:
            continue
        if isinstance(event, kind):
            return event
    raise AssertionError(f"no {kind.__name__} within {timeout}s")


def test_the_worker_owns_its_connection_and_client_and_speaks_only_through_events(server, pub, tmp_path, monkeypatch):
    main = threading.get_ident()
    opened, built = [], []
    real_connect, real_make = database.connect, service.make_client
    monkeypatch.setattr(database, "connect", lambda *a, **k: (opened.append(threading.get_ident()), real_connect(*a, **k))[1])
    monkeypatch.setattr(service, "make_client", lambda *a, **k: (built.append(threading.get_ident()), real_make(*a, **k))[1])
    worker = service.SyncWorker(worker_config(server, pub, tmp_path))
    assert isinstance(worker.events, queue.Queue)
    worker.start()
    try:
        finished = next_event(worker, service.SyncFinished)
        assert finished.result.outcome is Outcome.SNAPSHOT and finished.result.local_revision == 4
        assert opened and built and set(opened) != {main} and main not in opened and main not in built
        assert len(set(opened)) == 1 and set(opened) == set(built)
        worker.request_icon("equipment", W1)
        ready = next_event(worker, service.IconReady)
        assert (ready.kind, ready.sha256) == ("equipment", W1) and Path(ready.path).read_bytes() == pf.ICON_BYTES[W1]
        worker.request_icon("equipment", "ab" * 32)
        failed = next_event(worker, service.IconFailed)
        assert (failed.kind, failed.sha256) == ("equipment", "ab" * 32)
        worker.request_sync()
        assert next_event(worker, service.SyncFinished).result.outcome is Outcome.UP_TO_DATE
    finally:
        worker.stop()
    assert not [t for t in threading.enumerate() if t.name == "ul-house-sync"]
    conn = database.open_database(tmp_path / "w.sqlite")
    try:
        assert database.applied_revision(conn) == 4
    finally:
        conn.close()


def test_a_bad_icon_address_is_refused_on_the_calling_thread(server, pub, tmp_path):
    worker = service.SyncWorker(worker_config(server, pub, tmp_path))
    for kind, sha in (("packs", W1), ("equipment", "../x"), ("equipment", W1.upper())):
        with pytest.raises(ValueError):
            worker.request_icon(kind, sha)


def test_stop_joins_within_its_timeout_while_a_request_is_in_flight(server, pub, tmp_path):
    server.route("/v1/version", Reply(200, pub.read("current.json"), {"Content-Type": "application/json", "X-API-Commit": "dev",
                                                                       "ETag": '"slow"'}, delay=1.5))
    worker = service.SyncWorker(worker_config(server, pub, tmp_path))
    worker.start()
    deadline = time.monotonic() + 10
    while not server.hits("/v1/version") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.hits("/v1/version"), "the worker never asked"
    started = time.monotonic()
    worker.stop(timeout=5.0)
    assert time.monotonic() - started < 5.0
    assert not [t for t in threading.enumerate() if t.name == "ul-house-sync"]
    assert not server.hits("/v1/manifests/4")


def test_an_unexpected_exception_becomes_a_failed_result_and_the_thread_keeps_serving(server, pub, tmp_path, monkeypatch):
    real = service.sync_once
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk on fire")
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "sync_once", flaky)
    worker = service.SyncWorker(worker_config(server, pub, tmp_path))
    worker.start()
    try:
        first = next_event(worker, service.SyncFinished)
        assert first.result.outcome is Outcome.FAILED and "disk on fire" in (first.result.reason or "")
        assert [t for t in threading.enumerate() if t.name == "ul-house-sync"]       # still alive
        worker.request_sync()
        second = next_event(worker, service.SyncFinished)
        assert second.result.outcome is Outcome.SNAPSHOT
    finally:
        worker.stop()


def test_a_worker_that_cannot_open_its_database_reports_and_ends(server, pub, tmp_path):
    bad = tmp_path / "not-ours.sqlite"
    import sqlite3

    raw = sqlite3.connect(bad)
    raw.execute("CREATE TABLE other (a)")
    raw.commit()
    raw.close()
    worker = service.SyncWorker(config_for(server, tmp_path, db_path=bad, public_keys=(pub.public_hex,)))
    worker.start()
    try:
        event = next_event(worker, service.SyncFinished)
        assert event.result.outcome is Outcome.FAILED and "NotOurDatabase" in (event.result.reason or "")
    finally:
        worker.stop()
    assert server.log == []


class LateRng(random.Random):
    """always draws range top -> full jitter"""

    def uniform(self, a, b):
        return b


def test_the_launch_poll_waits_for_the_jitter(server, pub, tmp_path):
    config = config_for(server, tmp_path, public_keys=(pub.public_hex,), poll_interval=3600.0, launch_jitter=1.0)
    worker = service.SyncWorker(config, schedule=Schedule(3600.0, rng=LateRng()))
    started = time.monotonic()
    worker.start()
    try:
        next_event(worker, service.SyncFinished)
        assert time.monotonic() - started >= 0.9
    finally:
        worker.stop()


def test_the_snapshot_tmp_dir_defaults_next_to_the_database(tmp_path):
    config = config_for(LocalServerStub, tmp_path, db_path=tmp_path / "data" / "ul.sqlite")
    assert config.tmp_dir is None
    assert config.snapshot_tmp_dir == tmp_path / "data" / "tmp"
    explicit = config_for(LocalServerStub, tmp_path, db_path=tmp_path / "data" / "ul.sqlite", tmp_dir=tmp_path / "elsewhere")
    assert explicit.snapshot_tmp_dir == tmp_path / "elsewhere"


@pytest.mark.parametrize("explicit", [False, True])
def test_the_worker_downloads_snapshots_into_the_configured_tmp_dir(server, pub, tmp_path, explicit):
    data = tmp_path / "data"
    data.mkdir()
    extra = {"tmp_dir": tmp_path / "custom-tmp"} if explicit else {}
    config = config_for(server, tmp_path, db_path=data / "ul.sqlite", public_keys=(pub.public_hex,), poll_interval=3600.0, **extra)
    seen = []
    import ul_house.sync.snapshot as snap_module

    real = snap_module._install
    snap_module._install = lambda *a, **k: (seen.append(Path(k["tmp_dir"])), real(*a, **k))[1]
    worker = service.SyncWorker(config)
    worker.start()
    try:
        assert next_event(worker, service.SyncFinished).result.outcome is Outcome.SNAPSHOT
    finally:
        worker.stop()
        snap_module._install = real
    expected = tmp_path / "custom-tmp" if explicit else data / "tmp"
    assert seen == [expected] and expected.is_dir()
    assert not (data / "tmp").exists() or explicit is False
    assert list(expected.iterdir()) == []                       # download is deleted afterwards


DISPLAY_KEYS = {"catalog_version", "source_last_updated", "dataset_generated_at", "published_at"}
SYNC_STATE_KEYS = {"current_etag", "expires_at", "last_checked_at"} | DISPLAY_KEYS


@pytest.mark.parametrize("start,outcome", [(1, Outcome.UPDATED), (None, Outcome.SNAPSHOT)])
def test_the_display_keys_are_written_after_an_apply(db, server, pub, tmp_path, start, outcome):
    conn = db(start)
    assert sync(conn, server, pub, tmp_path).outcome is outcome
    manifest, current = pub.manifest(), pub.current()
    assert database.get_state(conn, "catalog_version") == manifest["build"]["catalog_version"] == "cd" * 32
    assert database.get_state(conn, "source_last_updated") == manifest["freshness"]["source_last_updated"]
    assert database.get_state(conn, "dataset_generated_at") == manifest["freshness"]["dataset_generated_at"]
    assert database.get_state(conn, "published_at") == current["published_at"] == pf.PUBLISHED_AT
    keys = {k for (k,) in conn.execute("SELECT key FROM sync_state")}
    assert keys == SYNC_STATE_KEYS
    assert not [k for k in keys if "revision" in k]
    assert not [v for (v,) in conn.execute("SELECT value FROM sync_state") if v in ("4", "1")]


def test_the_display_keys_are_not_written_when_nothing_was_applied(db, server, pub, tmp_path):
    server.route("/v1/snapshots/4", Reply(200, b"corrupt", {"Content-Type": "application/gzip", "X-API-Commit": "dev"}))
    published = pf.build_pub(tmp_path / "p2", package_hook=add_bad_op, validate=False)
    pf.serve(published, server)
    server.route("/v1/snapshots/4", Reply(200, b"corrupt", {"Content-Type": "application/gzip", "X-API-Commit": "dev"}))
    conn = db(1)
    assert sync(conn, server, published, tmp_path).outcome is Outcome.FAILED
    assert not ({k for (k,) in conn.execute("SELECT key FROM sync_state")} & DISPLAY_KEYS)


def test_a_new_release_replaces_the_display_keys(db, server, pub, tmp_path):
    conn = db(1)
    database.set_state(conn, {"catalog_version": "old", "published_at": "2020-01-01T00:00:00Z"})
    sync(conn, server, pub, tmp_path)
    assert database.get_state(conn, "catalog_version") == "cd" * 32 and database.get_state(conn, "published_at") == pf.PUBLISHED_AT


def collect(conn, server, pub, tmp_path, **kw):
    events = []
    result = sync(conn, server, pub, tmp_path, emit=events.append, **kw)
    return events, result


@pytest.mark.parametrize("start,kind,outcome", [(1, "update", Outcome.UPDATED), (None, "snapshot", Outcome.SNAPSHOT)])
def test_events_come_in_order_and_applying_precedes_the_transaction(db, server, pub, tmp_path, start, kind, outcome):
    conn = db(start)
    before = pf.everything(conn, rowid=True)
    statements, observed, events = [], [], []

    def emit(event):
        events.append(event)
        if isinstance(event, service.Applying):
            observed.append((pf.everything(conn, rowid=True) == before, conn.in_transaction,
                             any(s.lstrip().upper().startswith("BEGIN") for s in statements)))

    conn.set_trace_callback(statements.append)
    result = sync(conn, server, pub, tmp_path, emit=emit)
    conn.set_trace_callback(None)
    assert result.outcome is outcome
    assert events == [service.SyncStarted(), service.Applying(kind)]
    assert observed == [(True, False, False)]
    assert any(s.lstrip().upper().startswith("BEGIN") for s in statements)
    assert pf.everything(conn, rowid=True) != before


def test_a_fallback_cycle_announces_both_applies(db, server, tmp_path):
    published = pf.build_pub(tmp_path / "p", package_hook=add_bad_op, validate=False)
    pf.serve(published, server)
    events, result = collect(db(1), server, published, tmp_path)
    assert result.outcome is Outcome.SNAPSHOT
    assert events == [service.SyncStarted(), service.Applying("update"), service.Applying("snapshot")]


def test_cycles_that_apply_nothing_only_announce_the_start(db, server, pub, tmp_path):
    events, result = collect(db(4), server, pub, tmp_path)
    assert result.outcome is Outcome.UP_TO_DATE and events == [service.SyncStarted()]
    stranger = pf.make_keypair("stranger")
    refused = pf.build_pub(tmp_path / "p2", key=stranger)
    with LocalServer() as other:
        pf.serve(refused, other)
        events, result = collect(db(1), other, refused, tmp_path, keys=verify.load_public_keys([pf.KEY.public_hex]))
    assert result.outcome is Outcome.REFUSED and events == [service.SyncStarted()]


def events_until_finished(worker, timeout=20.0):
    seen = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = worker.events.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(event)
        if isinstance(event, service.SyncFinished):
            return seen
    raise AssertionError("no SyncFinished")


def test_the_worker_queue_carries_started_applying_finished_in_order(server, pub, tmp_path):
    worker = service.SyncWorker(worker_config(server, pub, tmp_path))
    worker.start()
    try:
        seen = events_until_finished(worker)
    finally:
        worker.stop()
    sequence = [type(e).__name__ for e in seen]
    assert sequence == ["SyncStarted", "Applying", "SyncFinished"], sequence
    assert seen[1].kind == "snapshot" and seen[2].result.outcome is Outcome.SNAPSHOT


def test_the_worker_orders_an_update_cycle_too(server, pub, tmp_path):
    seed = pf.client_db_at(tmp_path / "w.sqlite", 1)
    seed.close()
    worker = service.SyncWorker(worker_config(server, pub, tmp_path))
    worker.start()
    try:
        seen = events_until_finished(worker)
    finally:
        worker.stop()
    assert [type(e).__name__ for e in seen] == ["SyncStarted", "Applying", "SyncFinished"]
    assert seen[1].kind == "update" and seen[2].result.outcome is Outcome.UPDATED


def test_the_event_classes_are_importable_from_the_package():
    from ul_house import sync as package

    for name in ("SyncStarted", "Applying", "SyncFinished", "IconReady", "IconFailed"):
        assert getattr(package, name) is getattr(service, name), name
    assert service.Applying("update").kind == "update"


# lazy icon requests
def icon_route(server, sha, *, delay=0.0, status=200):
    body = pf.ICON_BYTES.get(sha, b"") if status == 200 else b""
    server.route(f"/v1/icons/equipment/{sha}", Reply(status, body, {"Content-Type": "image/png", "X-API-Commit": "dev"}, delay=delay))


def drain(worker, kind, count, timeout=20.0):
    return [next_event(worker, kind, timeout) for _ in range(count)]


def test_repeated_lazy_requests_for_one_icon_are_one_fetch(server, pub, tmp_path):
    icon_route(server, W1, delay=0.6)
    worker = service.SyncWorker(worker_config(server, pub, tmp_path))
    worker.start()
    try:
        next_event(worker, service.SyncFinished)
        for _ in range(5):
            worker.request_icon("equipment", W1)
        ready = next_event(worker, service.IconReady)
        time.sleep(1.0)
    finally:
        worker.stop()
    assert len(server.hits(f"/v1/icons/equipment/{W1}")) == 1
    assert ready.sha256 == W1
    leftover = []
    while not worker.events.empty():
        leftover.append(worker.events.get_nowait())
    assert not [e for e in leftover if isinstance(e, (service.IconReady, service.IconFailed))]


def test_repeated_requests_for_a_missing_icon_fail_once(server, pub, tmp_path):
    unknown = "ab" * 32
    server.route(f"/v1/icons/equipment/{unknown}", Reply(404, b"", {"X-API-Commit": "dev"}, delay=0.5))
    worker = service.SyncWorker(worker_config(server, pub, tmp_path))
    worker.start()
    try:
        next_event(worker, service.SyncFinished)
        for _ in range(4):
            worker.request_icon("equipment", unknown)
        failed = next_event(worker, service.IconFailed)
        time.sleep(0.8)
    finally:
        worker.stop()
    assert failed.sha256 == unknown and len(server.hits(f"/v1/icons/equipment/{unknown}")) == 1


def test_different_icons_are_fetched_separately_and_a_cached_one_is_free(server, pub, tmp_path):
    w2 = pf.icon_sha("w2")
    worker = service.SyncWorker(worker_config(server, pub, tmp_path))
    worker.start()
    try:
        next_event(worker, service.SyncFinished)
        worker.request_icon("equipment", W1)
        worker.request_icon("equipment", w2)
        got = {e.sha256 for e in drain(worker, service.IconReady, 2)}
        worker.request_icon("equipment", W1)
        again = next_event(worker, service.IconReady)
    finally:
        worker.stop()
    assert got == {W1, w2} and again.sha256 == W1
    assert len(server.hits(f"/v1/icons/equipment/{W1}")) == 1 and len(server.hits(f"/v1/icons/equipment/{w2}")) == 1


@pytest.mark.parametrize("start", [1, None])
def test_a_locked_database_is_a_failed_cycle_without_a_snapshot_fallback(db, server, pub, tmp_path, start):
    conn = db(start, name="busy.sqlite")
    conn.execute("PRAGMA busy_timeout = 50")
    before = pf.everything(conn, rowid=True)
    blocker = database.connect(tmp_path / "busy.sqlite")
    blocker.execute("BEGIN IMMEDIATE")
    try:
        result = sync(conn, server, pub, tmp_path)
        assert result.outcome is Outcome.FAILED
        if start is not None:
            assert result.reason == "busy"
        else:
            assert "busy" in result.reason or "locked" in result.reason
        if start is not None:
            assert server.hits("/v1/snapshots/4") == []
        assert not conn.in_transaction
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert pf.everything(conn, rowid=True) == before
    assert database.get_state(conn, "current_etag") is None
    again = sync(conn, server, pub, tmp_path)
    assert again.outcome in (Outcome.UPDATED, Outcome.SNAPSHOT) and database.applied_revision(conn) == 4
