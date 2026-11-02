import hashlib

import pytest

from release_fixtures import FakeS3Client, FakeS3Error

from ul_house.build.store import (
    ImmutableConflict,
    IntegrityError,
    Layout,
    LocalStore,
    ObjectInfo,
    ObjectMissing,
    R2Store,
    StoreError,
    check_key,
    open_store,
    put_immutable,
    sha256_bytes,
    sha256_file,
    verify_object,
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(params=["local", "r2"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalStore(tmp_path / "root")
    return R2Store(FakeS3Client(), "ul-house")


def test_put_get_head_roundtrip(store):
    info = store.put("pub/a/b.json", b"hello", content_type="application/json")
    assert info == ObjectInfo("pub/a/b.json", 5, digest(b"hello"))
    assert store.get("pub/a/b.json") == b"hello"
    head = store.head("pub/a/b.json")
    assert head is not None and head.size == 5 and head.sha256 == digest(b"hello")


def test_head_of_absent_key_is_none(store):
    assert store.head("pub/nothing") is None


def test_get_of_absent_key_raises_object_missing(store, tmp_path):
    with pytest.raises(ObjectMissing):
        store.get("pub/nothing")
    with pytest.raises(ObjectMissing):
        store.get_file("pub/nothing", tmp_path / "dest")
    assert not (tmp_path / "dest").exists()


def test_object_missing_is_a_key_error_and_a_store_error(store):
    with pytest.raises(KeyError):
        store.get("pub/nothing")
    with pytest.raises(StoreError):
        store.get("pub/nothing")


def test_put_overwrites(store):
    store.put("k/x", b"one")
    store.put("k/x", b"two")
    assert store.get("k/x") == b"two"
    assert store.head("k/x").sha256 == digest(b"two")


def test_list_is_sorted_and_filtered_by_prefix(store):
    for key in ("pub/b", "pub/a", "build/x", "pub/sub/c", "candidates/1/bundle.json"):
        store.put(key, key.encode())
    assert [o.key for o in store.list("pub/")] == ["pub/a", "pub/b", "pub/sub/c"]
    assert [o.key for o in store.list()] == [
        "build/x", "candidates/1/bundle.json", "pub/a", "pub/b", "pub/sub/c"]
    assert store.list("nothing/") == []
    sizes = {o.key: o.size for o in store.list()}
    assert sizes["pub/sub/c"] == len(b"pub/sub/c")


def test_delete_removes_and_absent_is_not_an_error(store):
    store.put("pub/a", b"x")
    store.delete("pub/a")
    assert store.head("pub/a") is None
    store.delete("pub/a")
    store.delete("pub/never-existed")


def test_get_with_wrong_sha_raises_integrity_error(store):
    store.put("pub/a", b"payload")
    with pytest.raises(IntegrityError):
        store.get("pub/a", sha256=digest(b"something else"))
    assert store.get("pub/a", sha256=digest(b"payload")) == b"payload"


def test_put_file_and_get_file_roundtrip(store, tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"\x00\x01binary" * 1000)
    info = store.put_file("build/seed/current.sqlite", src, content_type="application/vnd.sqlite3")
    assert info.sha256 == sha256_file(src) and info.size == src.stat().st_size
    dest = tmp_path / "out" / "nested" / "copy.bin"
    got = store.get_file("build/seed/current.sqlite", dest, sha256=info.sha256)
    assert dest.read_bytes() == src.read_bytes()
    assert got.sha256 == info.sha256 and got.size == info.size


def test_get_file_with_wrong_sha_leaves_no_partial_dest(store, tmp_path):
    store.put("pub/a", b"payload")
    dest = tmp_path / "dest.bin"
    with pytest.raises(IntegrityError):
        store.get_file("pub/a", dest, sha256="0" * 64)
    assert not dest.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name != "root"] == []


def test_get_file_with_wrong_sha_keeps_an_existing_dest_intact(store, tmp_path):
    store.put("pub/a", b"payload")
    dest = tmp_path / "dest.bin"
    dest.write_bytes(b"old content")
    with pytest.raises(IntegrityError):
        store.get_file("pub/a", dest, sha256="0" * 64)
    assert dest.read_bytes() == b"old content"


def test_put_immutable_same_bytes_is_a_no_op(store):
    info, uploaded = put_immutable(store, "pub/snapshots/unison-1.sqlite.gz", b"bytes")
    assert uploaded is True and info.sha256 == digest(b"bytes")
    again, uploaded = put_immutable(store, "pub/snapshots/unison-1.sqlite.gz", b"bytes")
    assert uploaded is False and again.sha256 == info.sha256


def test_put_immutable_different_bytes_conflicts_and_changes_nothing(store):
    put_immutable(store, "pub/manifests/1.json", b"original")
    with pytest.raises(ImmutableConflict):
        put_immutable(store, "pub/manifests/1.json", b"different")
    assert store.get("pub/manifests/1.json") == b"original"


def test_put_immutable_accepts_a_path(store, tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"from a file")
    info, uploaded = put_immutable(store, "pub/x", f)
    assert uploaded and info.sha256 == digest(b"from a file")
    _, uploaded = put_immutable(store, "pub/x", f)
    assert not uploaded
    f.write_bytes(b"changed")
    with pytest.raises(ImmutableConflict):
        put_immutable(store, "pub/x", f)


def test_verify_object(store):
    store.put("pub/a", b"abc")
    verify_object(store, "pub/a", digest(b"abc"))
    with pytest.raises(IntegrityError):
        verify_object(store, "pub/a", "f" * 64)
    with pytest.raises(ObjectMissing):
        verify_object(store, "pub/absent", digest(b"abc"))


@pytest.mark.parametrize("key", ["../x", "/x", "a//b", "a\\b", "a/../b", "a/./b", ""])
def test_bad_keys_are_rejected_everywhere(store, key):
    with pytest.raises(ValueError):
        store.put(key, b"x")
    with pytest.raises(ValueError):
        store.get(key)
    with pytest.raises(ValueError):
        store.head(key)
    with pytest.raises(ValueError):
        store.delete(key)


@pytest.mark.parametrize("key", ["../x", "/x", "a//b", "a\\b", "a/../b", "a/./b", ""])
def test_check_key_rejects(key):
    with pytest.raises(ValueError):
        check_key(key)


@pytest.mark.parametrize("key", ["pub/current.json", "a", "build/history/archived/2027-Q1.duckdb"])
def test_check_key_accepts_layout_shaped_keys(key):
    assert check_key(key) == key


def test_hash_helpers(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"x" * (3 * (1 << 20) + 5))                 # more than one 1 MiB chunk
    assert sha256_file(f) == digest(f.read_bytes()) == sha256_bytes(f.read_bytes())


# local store specifics
def test_local_layout_is_the_key_path(tmp_path):
    s = LocalStore(tmp_path / "root")
    s.put("pub/manifests/1.json", b"{}")
    assert (tmp_path / "root" / "pub" / "manifests" / "1.json").read_bytes() == b"{}"
    assert s.pub_dir == tmp_path / "root" / "pub"
    assert s.kind == "local"


def test_local_writes_are_atomic_and_leave_no_tmp_entries(tmp_path):
    s = LocalStore(tmp_path / "root")
    s.put("pub/a", b"1")
    src = tmp_path / "src"
    src.write_bytes(b"2")
    s.put_file("pub/b", src)
    assert [o.key for o in s.list()] == ["pub/a", "pub/b"]
    tmp = tmp_path / "root" / ".tmp"
    assert not tmp.exists() or list(tmp.iterdir()) == []


def test_local_failed_get_file_leaves_no_part_files(tmp_path):
    s = LocalStore(tmp_path / "root")
    s.put("pub/a", b"payload")
    dest_dir = tmp_path / "dl"
    with pytest.raises(IntegrityError):
        s.get_file("pub/a", dest_dir / "a", sha256="1" * 64)
    assert list(dest_dir.iterdir()) == []


def test_local_delete_prunes_directories_it_emptied(tmp_path):
    s = LocalStore(tmp_path / "root")
    s.put("candidates/7/pub/manifests/7.json", b"{}")
    s.delete("candidates/7/pub/manifests/7.json")
    assert not (tmp_path / "root" / "candidates" / "7").exists()
    assert (tmp_path / "root").exists()


def test_local_bad_key_writes_nothing(tmp_path):
    s = LocalStore(tmp_path / "root")
    with pytest.raises(ValueError):
        s.put("../escape", b"x")
    assert not (tmp_path / "escape").exists()


# r2 over fake s3 client
def test_r2_records_sha256_metadata_and_content_type():
    client = FakeS3Client()
    s = R2Store(client, "ul-house")
    s.put("pub/a.png", b"png", content_type="image/png")
    stored = client.objects[("ul-house", "pub/a.png")]
    assert stored["meta"] == {"sha256": digest(b"png")}
    assert stored["type"] == "image/png"
    assert s.kind == "r2"


def test_r2_list_follows_pagination():
    client = FakeS3Client(max_keys=2)
    s = R2Store(client, "ul-house")
    keys = [f"pub/icons/equipment/{i:02d}.png" for i in range(7)]
    for key in keys:
        s.put(key, key.encode())
    s.put("build/x", b"x")
    assert [o.key for o in s.list("pub/icons/")] == keys
    pages = [c for c in client.calls if c[0] == "list_objects_v2"]
    assert len(pages) >= 4                                   # 7 keys at 2 per page


def test_r2_list_carries_no_sha256():
    s = R2Store(FakeS3Client(), "ul-house")
    s.put("pub/a", b"x")
    assert [o.sha256 for o in s.list("pub/")] == [None]


def test_r2_head_uses_metadata_and_tolerates_missing_metadata():
    client = FakeS3Client()
    s = R2Store(client, "ul-house")
    client.objects[("ul-house", "pub/foreign")] = {"body": b"abc", "meta": {}, "type": None}
    head = s.head("pub/foreign")
    assert head is not None and head.size == 3 and head.sha256 is None


def test_r2_put_immutable_compares_by_reading_when_metadata_is_absent():
    client = FakeS3Client()
    s = R2Store(client, "ul-house")
    client.objects[("ul-house", "pub/foreign")] = {"body": b"abc", "meta": {}, "type": None}
    _, uploaded = put_immutable(s, "pub/foreign", b"abc")
    assert uploaded is False
    with pytest.raises(ImmutableConflict):
        put_immutable(s, "pub/foreign", b"xyz")


def test_r2_missing_codes_map_to_object_missing():
    class Client(FakeS3Client):
        def get_object(self, **kw):
            raise FakeS3Error("404")
    with pytest.raises(ObjectMissing):
        R2Store(Client(), "b").get("pub/x")


def test_r2_other_errors_propagate():
    class Client(FakeS3Client):
        def head_object(self, **kw):
            raise FakeS3Error("500", "boom")
        def get_object(self, **kw):
            raise FakeS3Error("AccessDenied")
    s = R2Store(Client(), "b")
    with pytest.raises(FakeS3Error):
        s.head("pub/x")
    with pytest.raises(FakeS3Error):
        s.get("pub/x")


def test_r2_put_file_streams_the_open_file(tmp_path):
    client = FakeS3Client()
    s = R2Store(client, "ul-house")
    f = tmp_path / "big"
    f.write_bytes(b"z" * 5000)
    info = s.put_file("build/history/current.duckdb", f)
    assert info.size == 5000 and client.objects[("ul-house", "build/history/current.duckdb")]["body"] == b"z" * 5000


def test_r2_from_env_names_the_missing_variable_only(monkeypatch):
    with pytest.raises(StoreError) as exc:
        R2Store.from_env({"R2_ACCESS_KEY_ID": "SECRETVALUE", "R2_SECRET_ACCESS_KEY": "SECRETVALUE2"})
    assert "R2_ENDPOINT" in str(exc.value)
    assert "SECRETVALUE" not in str(exc.value)


def test_r2_from_env_builds_a_client_without_touching_the_network():
    s = R2Store.from_env({"R2_ENDPOINT": "https://acct.r2.cloudflarestorage.com",
                          "R2_ACCESS_KEY_ID": "id", "R2_SECRET_ACCESS_KEY": "secret"})
    assert s.bucket == "ul-house"
    s2 = R2Store.from_env({"R2_ENDPOINT": "https://acct.r2.cloudflarestorage.com",
                           "R2_ACCESS_KEY_ID": "id", "R2_SECRET_ACCESS_KEY": "secret", "R2_BUCKET": "other"})
    assert s2.bucket == "other"


# open store
def test_open_store_local(tmp_path):
    s = open_store(f"local:{tmp_path / 'x'}")
    assert isinstance(s, LocalStore) and s.root == tmp_path / "x"


def test_open_store_r2_without_environment_names_the_variable():
    with pytest.raises(StoreError, match="R2_"):
        open_store("r2", {})


@pytest.mark.parametrize("uri", ["ftp:x", "", "local:", "s3", "R2"])
def test_open_store_rejects_everything_else(uri):
    with pytest.raises(ValueError):
        open_store(uri, {})


# layout
def test_layout_keys_match_section_13_1():
    assert Layout.SEED_CURRENT == "build/seed/current.sqlite"
    assert Layout.HISTORY_CURRENT == "build/history/current.duckdb"
    assert Layout.CHECKPOINT == "build/history/checkpoint.json"
    assert Layout.CURRENT == "pub/current.json"
    assert Layout.seed_at(1847) == "build/seed/1847.sqlite"
    assert Layout.history_at(1847) == "build/history/1847.duckdb"
    assert Layout.history_backup("20270102T030000Z") == "build/history/backups/pre-20270102T030000Z.duckdb"
    assert Layout.archive("2027-Q1") == "build/history/archived/2027-Q1.duckdb"
    assert Layout.candidate(1847) == "candidates/1847/"
    assert Layout.rel_manifest(1847) == "manifests/1847.json"
    assert Layout.rel_snapshot(1847) == "snapshots/unison-1847.sqlite.gz"
    assert Layout.rel_update(1841, 1847) == "updates/1841-1847.json.gz"
    assert Layout.rel_icon("equipment", "9f" * 32) == "icons/equipment/" + "9f" * 32 + ".png"
    assert Layout.rel_base_pack(1847) == "icons/packs/base-1847.zip"
    assert Layout.rel_delta_pack(1841, 1847) == "icons/packs/delta-1841-1847.zip"
    assert Layout.rel_lineage(1847) == "lineage/1847.json.gz"
    assert Layout.pub("manifests/1847.json") == "pub/manifests/1847.json"


def test_layout_parsers_invert_the_builders():
    sha = "ab" * 32
    assert Layout.parse_manifest_key(Layout.pub(Layout.rel_manifest(12))) == 12
    assert Layout.parse_snapshot_key(Layout.pub(Layout.rel_snapshot(12))) == 12
    assert Layout.parse_update_key(Layout.pub(Layout.rel_update(3, 12))) == (3, 12)
    assert Layout.parse_icon_key(Layout.pub(Layout.rel_icon("item", sha))) == ("item", sha)
    assert Layout.parse_base_pack_key(Layout.pub(Layout.rel_base_pack(12))) == 12
    assert Layout.parse_delta_pack_key(Layout.pub(Layout.rel_delta_pack(3, 12))) == (3, 12)
    assert Layout.parse_lineage_key(Layout.pub(Layout.rel_lineage(12))) == 12
    assert Layout.parse_seed_key(Layout.seed_at(12)) == 12
    assert Layout.parse_history_key(Layout.history_at(12)) == 12
    assert Layout.parse_backup_key(Layout.history_backup("20270102T030000Z")) == "20270102T030000Z"
    assert Layout.parse_candidate_key("candidates/12/pub/manifests/12.json") == (12, "pub/manifests/12.json")


@pytest.mark.parametrize("parse, key", [
    (Layout.parse_manifest_key, "pub/manifests/x.json"),
    (Layout.parse_manifest_key, "pub/manifests/0.json"),
    (Layout.parse_snapshot_key, "pub/snapshots/unison-.sqlite.gz"),
    (Layout.parse_update_key, "pub/updates/1.json.gz"),
    (Layout.parse_seed_key, "build/seed/current.sqlite"),
    (Layout.parse_history_key, "build/history/current.duckdb"),
    (Layout.parse_icon_key, "pub/icons/equipment/short.png"),
    (Layout.parse_candidate_key, "candidates/x/bundle.json"),
])
def test_layout_parsers_return_none_for_other_shapes(parse, key):
    assert parse(key) is None
