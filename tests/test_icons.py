"""The icon cache, verified by sha256."""
import hashlib
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

import pub_fixture as pf
from local_server import LocalServer, Reply
from ul_house.sync import icons, verify
from ul_house.sync.errors import IconError, VerificationError

W1, W2, G2 = (pf.icon_sha(n) for n in ("w1", "w2", "g2"))


@pytest.fixture
def cache(tmp_path):
    return icons.IconCache(tmp_path / "cache")


@pytest.fixture
def server():
    with LocalServer() as running:
        yield running


@pytest.fixture
def kinds():
    """sha256 to kind, from icon table."""
    return {row["sha256"]: row["kind"] for row in pf.make_states()[4]["icon"]}


def files_under(root: Path):
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def pack_of(members, *, name="delta-1-4.zip", index=None, shas=None):
    """zip: index.json first, then members."""
    shas = list(shas if shas is not None else [])
    listing = index if index is not None else pf.pack_index(name, "delta", 1, 4, shas)
    return pf.zip_members([("index.json", json.dumps(listing).encode())] + list(members))


# store and load
def test_store_and_load_round_trip(cache):
    data = pf.ICON_BYTES[W1]
    path = cache.store("equipment", W1, data)
    assert path == cache.path_for("equipment", W1) == cache.root / "equipment" / f"{W1}.png"
    assert path.read_bytes() == data and cache.has("equipment", W1)
    assert cache.load("equipment", W1) == data
    assert cache.load("equipment", W2) is None and not cache.has("equipment", W2)
    assert [p.name for p in files_under(cache.root)] == [f"{W1}.png"]               # no temp file left behind


def test_a_hash_mismatch_is_never_written(cache):
    with pytest.raises(IconError):
        cache.store("equipment", W1, pf.ICON_BYTES[W2])
    with pytest.raises(IconError):
        cache.store("equipment", W1, b"")
    assert files_under(cache.root) == []


def test_oversize_icons_are_refused(cache, monkeypatch):
    monkeypatch.setattr(icons, "MAX_ICON_BYTES", 10)
    with pytest.raises(IconError):
        cache.store("equipment", W1, pf.ICON_BYTES[W1])
    assert files_under(cache.root) == []


def test_a_tampered_cached_file_is_deleted_on_load(cache):
    path = cache.store("equipment", W1, pf.ICON_BYTES[W1])
    path.write_bytes(b"not the icon any more")
    assert cache.load("equipment", W1) is None
    assert not path.exists()
    assert cache.load("equipment", W1) is None                                       # and stays absent


@pytest.mark.parametrize("kind,sha", [
    ("packs", W1), ("../x", W1), ("", W1), ("EQUIPMENT", W1), ("equipment/..", W1),
    ("equipment", W1.upper()), ("equipment", W1[:-1]), ("equipment", W1 + "0"), ("equipment", "../" + W1[3:]),
    ("equipment", "g" * 64), ("equipment", ""), ("equipment", W1 + "\n"),
])
def test_addresses_are_validated(cache, kind, sha):
    with pytest.raises(ValueError):
        cache.path_for(kind, sha)
    with pytest.raises(ValueError):
        cache.load(kind, sha)


def test_every_vocabulary_kind_has_a_directory(cache):
    for kind in ("equipment", "item", "ability"):
        assert cache.path_for(kind, W1).parent.name == kind


def test_a_failing_replace_leaves_no_final_file_and_no_temp(cache, monkeypatch):
    seen = []
    real = os.replace

    def failing(src, dst):
        seen.append((Path(src).exists(), Path(dst).exists()))
        raise OSError("disk says no")

    monkeypatch.setattr(os, "replace", failing)
    with pytest.raises(OSError):
        cache.store("equipment", W1, pf.ICON_BYTES[W1])
    monkeypatch.setattr(os, "replace", real)
    assert seen == [(True, False)]                              # bytes complete in temp; final missing
    assert files_under(cache.root) == []                        # temp removed, final never appeared
    assert cache.load("equipment", W1) is None


def test_the_final_path_only_ever_holds_complete_files(cache, monkeypatch):
    real = os.replace
    observed = []

    def checking(src, dst):
        assert not Path(dst).exists()                           # nothing partial under the final name
        assert Path(src).read_bytes() == pf.ICON_BYTES[W1]      # temp file complete and synced first
        observed.append(Path(src).name)
        return real(src, dst)

    monkeypatch.setattr(os, "replace", checking)
    cache.store("equipment", W1, pf.ICON_BYTES[W1])
    assert len(observed) == 1 and observed[0].startswith(f".{W1}.") and observed[0].endswith(".tmp")


def test_two_writers_of_the_same_icon_leave_one_valid_file(cache):
    errors = []

    def writer():
        try:
            for _ in range(25):
                cache.store("equipment", W1, pf.ICON_BYTES[W1])
        except BaseException as exc:                             # pragma: no cover - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [p.name for p in files_under(cache.root)] == [f"{W1}.png"]
    assert cache.load("equipment", W1) == pf.ICON_BYTES[W1]


# packs
def test_a_delta_pack_installs_exactly_the_added_icons(cache, kinds):
    added = pf.added_icons(1, 4)
    blob = pf.delta_pack_bytes(1, 4)
    result = cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds, wanted=added)
    assert sorted(result.installed) == sorted(added) == sorted([W2, G2])
    assert result.discarded == () and result.ignored == () and result.missing == ()
    assert {p.name for p in files_under(cache.root)} == {f"{s}.png" for s in added}
    assert all(cache.load("equipment", s) == pf.ICON_BYTES[s] for s in added)


def test_a_base_pack_installs_every_icon(cache, kinds):
    shas = sorted(kinds)
    blob = pf.make_pack("base-4.zip", "base", None, 4, shas)
    result = cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert sorted(result.installed) == shas and len(shas) == 7


def test_only_wanted_icons_are_installed(cache, kinds):
    blob = pf.delta_pack_bytes(1, 4)
    result = cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds, wanted=[W2])
    assert result.installed == (W2,) and len(result.ignored) == 1
    assert not cache.has("equipment", G2)


def test_a_pack_with_a_wrong_hash_installs_nothing(cache, kinds):
    blob = pf.delta_pack_bytes(1, 4)
    for expected in ("0" * 64, pf.sha256_hex(blob + b"x")):
        with pytest.raises(VerificationError):
            cache.install_pack(blob, expected_sha256=expected, icon_kinds=kinds)
    with pytest.raises(VerificationError):
        cache.install_pack(blob + b"x", expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert files_under(cache.root) == []


def test_a_corrupted_member_is_discarded_and_the_others_are_kept(cache, kinds):
    listing = pf.pack_index("delta-1-4.zip", "delta", 1, 4, [W2, G2])
    blob = pack_of([(f"equipment/{W2}.png", pf.ICON_BYTES[W2][:-1] + b"X"),
                    (f"equipment/{G2}.png", pf.ICON_BYTES[G2])], index=listing)
    result = cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert result.installed == (G2,) and result.discarded == (W2,)
    assert not cache.has("equipment", W2) and cache.load("equipment", G2) == pf.ICON_BYTES[G2]


def test_a_member_whose_size_disagrees_with_the_listing_is_discarded(cache, kinds):
    listing = pf.pack_index("delta-1-4.zip", "delta", 1, 4, [W2])
    blob = pack_of([(f"equipment/{W2}.png", pf.ICON_BYTES[W2] + b"\x00")], index=listing)
    result = cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert result.installed == () and result.discarded == (W2,)


def test_a_kind_that_disagrees_with_the_icon_table_is_discarded(cache, kinds):
    blob = pf.delta_pack_bytes(1, 4)
    wrong = dict(kinds, **{W2: "item"})
    result = cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=wrong)
    assert result.installed == (G2,) and result.discarded == (W2,)
    assert not files_under(cache.root / "item")


@pytest.mark.parametrize("name", [
    "../x.png", "/abs.png", f"equipment/../../{'a' * 64}.png", f"equipment/{W2}.txt", f"equipment/{W2}.png.exe",
    f"packs/{W2}.png", f"EQUIPMENT/{W2}.png", f"{W2}.png", f"equipment/{W2[:-1]}.png", f"equipment\\{W2}.png",
    f"./equipment/{W2}.png", "equipment/", "equipment",
])
def test_member_names_outside_the_layout_are_ignored(tmp_path, kinds, name):
    cache = icons.IconCache(tmp_path / "cache")
    before = set(tmp_path.rglob("*"))
    listing = pf.pack_index("delta-1-4.zip", "delta", 1, 4, [W2])
    blob = pack_of([(name, pf.ICON_BYTES[W2])], index=listing)
    result = cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert result.installed == ()
    assert files_under(cache.root) == []
    assert not (set(tmp_path.rglob("*")) - before - {cache.root}), "nothing was written outside the cache"
    assert not (tmp_path.parent / "x.png").exists() and not Path("/abs.png").exists()


def test_a_member_the_index_does_not_list_is_ignored(cache, kinds):
    listing = pf.pack_index("delta-1-4.zip", "delta", 1, 4, [W2])
    blob = pack_of([(f"equipment/{W2}.png", pf.ICON_BYTES[W2]), (f"equipment/{G2}.png", pf.ICON_BYTES[G2])], index=listing)
    result = cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert result.installed == (W2,) and len(result.ignored) == 1 and not cache.has("equipment", G2)


def test_a_listed_member_that_is_absent_is_reported_for_lazy_fetch(cache, kinds):
    listing = pf.pack_index("delta-1-4.zip", "delta", 1, 4, [W2, G2])
    blob = pack_of([(f"equipment/{W2}.png", pf.ICON_BYTES[W2])], index=listing)
    result = cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert result.installed == (W2,) and result.missing == (G2,)


def test_an_oversize_member_is_discarded(cache, kinds, monkeypatch):
    monkeypatch.setattr(icons, "MAX_ICON_BYTES", len(pf.ICON_BYTES[G2]) - 1)
    blob = pf.delta_pack_bytes(1, 4)
    result = cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert result.installed == () and sorted(result.discarded) == sorted([W2, G2])
    assert files_under(cache.root) == []


def test_a_pack_whose_index_is_not_first_installs_nothing(cache, kinds):
    listing = pf.pack_index("delta-1-4.zip", "delta", 1, 4, [W2])
    blob = pf.zip_members([(f"equipment/{W2}.png", pf.ICON_BYTES[W2]), ("index.json", json.dumps(listing).encode())])
    with pytest.raises(IconError):
        cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert files_under(cache.root) == []


@pytest.mark.parametrize("index_bytes", [b"not json", b"[]", b'{"name": "delta-1-4.zip"}', b"",
                                         json.dumps({"name": "delta-1-4.zip", "pack_type": "delta", "from_revision": 1,
                                                     "to_revision": 4, "icons": [{"sha256": "zz", "kind": "equipment",
                                                                                   "bytes": 1, "path": "x"}]}).encode()])
def test_an_invalid_index_installs_nothing(cache, kinds, index_bytes):
    blob = pf.zip_members([("index.json", index_bytes), (f"equipment/{W2}.png", pf.ICON_BYTES[W2])])
    with pytest.raises(IconError):
        cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert files_under(cache.root) == []


def test_a_pack_named_differently_from_the_request_installs_nothing(cache, kinds):
    blob = pf.delta_pack_bytes(1, 4)
    with pytest.raises(IconError):
        cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds, name="delta-2-4.zip")
    assert files_under(cache.root) == []
    result = cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds, name="delta-1-4.zip")
    assert len(result.installed) == 2


def test_something_that_is_not_a_zip_installs_nothing(cache, kinds):
    for blob in (b"not a zip at all", b"", pf.delta_pack_bytes(1, 4)[:60]):
        with pytest.raises(IconError):
            cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert files_under(cache.root) == []


def test_too_many_members_are_refused(cache, kinds, monkeypatch):
    monkeypatch.setattr(icons, "MAX_PACK_MEMBERS", 1)
    blob = pf.delta_pack_bytes(1, 4)
    with pytest.raises(IconError):
        cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds)
    assert files_under(cache.root) == []
    assert verify.MAX_PACK_MEMBERS == 100_000


def test_a_pack_is_never_extracted_through_zipfile_paths(cache, kinds, monkeypatch):
    """zip-slip: member names are never paths."""
    blob = pf.delta_pack_bytes(1, 4)
    monkeypatch.setattr(zipfile.ZipFile, "extract", lambda *a, **k: pytest.fail("extract() used"))
    monkeypatch.setattr(zipfile.ZipFile, "extractall", lambda *a, **k: pytest.fail("extractall() used"))
    assert len(cache.install_pack(blob, expected_sha256=pf.sha256_hex(blob), icon_kinds=kinds).installed) == 2


# lazy fetch
def test_lazy_fetch_stores_one_icon_with_one_request(tmp_path, cache, server):
    pub = pf.build_pub(tmp_path / "p")
    pf.serve(pub, server)
    client = pf.make_client(server)
    path = cache.fetch(client, "equipment", W1)
    assert path == cache.path_for("equipment", W1) and cache.load("equipment", W1) == pf.ICON_BYTES[W1]
    assert len(server.hits(f"/v1/icons/equipment/{W1}")) == 1
    assert cache.fetch(client, "equipment", W1) == path                   # cached: no second request
    assert len(server.hits(f"/v1/icons/equipment/{W1}")) == 1


def test_lazy_fetch_404_is_none(cache, server):
    unknown = "ab" * 32
    assert cache.fetch(pf.make_client(server), "equipment", unknown) is None
    assert len(server.hits(f"/v1/icons/equipment/{unknown}")) == 1
    assert files_under(cache.root) == []


def test_lazy_fetch_of_bytes_that_do_not_match_the_address_caches_nothing(cache, server):
    server.route(f"/v1/icons/equipment/{W1}", Reply(200, pf.ICON_BYTES[W2], {"Content-Type": "image/png", "X-API-Commit": "dev"}))
    assert cache.fetch(pf.make_client(server), "equipment", W1) is None
    assert files_under(cache.root) == []


def test_lazy_fetch_survives_server_errors_and_a_dead_server(cache, server):
    server.route(f"/v1/icons/equipment/{W1}", Reply(500, b"boom"))
    assert cache.fetch(pf.make_client(server), "equipment", W1) is None
    from ul_house.http.client import Client

    dead = Client("http://127.0.0.1:9", interval=0.0, max_retries=0, timeout=(0.5, 0.5))
    assert cache.fetch(dead, "equipment", W1) is None
    assert files_under(cache.root) == []


# db helpers
def test_missing_lists_referenced_icons_that_are_not_cached(tmp_path, cache):
    conn = pf.client_db_at(tmp_path / "c.sqlite", 4)
    try:
        everything = cache.missing(conn)
        assert len(everything) == 7 and all(k == "equipment" for k, _ in everything)
        cache.store("equipment", W1, pf.ICON_BYTES[W1])
        cache.store("equipment", W2, pf.ICON_BYTES[W2])
        assert len(cache.missing(conn)) == 5 and ("equipment", W1) not in cache.missing(conn)
        assert cache.icon_kinds(conn)[W1] == "equipment"
    finally:
        conn.close()


def make_file(path: Path, data=b"x", mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_cleanup_removes_only_what_it_owns_and_no_longer_references(tmp_path, cache):
    conn = pf.client_db_at(tmp_path / "c.sqlite", 4)
    try:
        referenced = pf.icon_sha("w0")
        cache.store("equipment", referenced, pf.ICON_BYTES[referenced])
        orphan_bytes = pf.make_png(77)
        orphan = pf.sha256_hex(orphan_bytes)
        cache.store("equipment", orphan, orphan_bytes) 
        now = 10_000_000.0
        old_tmp = make_file(cache.root / "equipment" / f".{referenced}.abc123.tmp", mtime=now - 7200)
        young_tmp = make_file(cache.root / "equipment" / f".{referenced}.def456.tmp", mtime=now - 10)
        foreign = make_file(cache.root / "equipment" / "notes.txt")
        upper = make_file(cache.root / "equipment" / ("E" * 64 + ".png"))
        outside = make_file(tmp_path / "outside.png", b"precious")
        link = cache.root / "equipment" / f"{'c' * 64}.png"
        try:
            os.symlink(outside, link)
        except OSError:
            link = None
        directory = cache.root / "equipment" / f"{'d' * 64}.png"
        directory.mkdir()
        stray = make_file(cache.root / "elsewhere" / f"{orphan}.png")
        removed = cache.cleanup(conn, now=lambda: now)
        assert not cache.has("equipment", orphan) and cache.has("equipment", referenced)
        assert not old_tmp.exists() and young_tmp.exists()
        assert foreign.exists() and stray.exists() and directory.is_dir()
        assert upper.exists()
        assert outside.read_bytes() == b"precious"
        if link is not None:
            assert link.is_symlink() and outside.exists()
        assert removed == 2
    finally:
        conn.close()


def test_cleanup_deletes_withdrawn_icons_even_when_referenced(tmp_path, cache):
    conn = pf.client_db_at(tmp_path / "c.sqlite", 4)
    try:
        for sha in (W1, W2):
            cache.store("equipment", sha, pf.ICON_BYTES[sha])
        assert cache.cleanup(conn, withdrawn=[W1]) == 1
        assert not cache.has("equipment", W1) and cache.has("equipment", W2)
        assert cache.cleanup(conn, withdrawn=[W1]) == 0
        assert cache.cleanup(conn) == 0
    finally:
        conn.close()


def test_cleanup_on_an_empty_cache_is_fine(tmp_path, cache):
    conn = pf.client_db_at(tmp_path / "c.sqlite", 4)
    try:
        assert cache.cleanup(conn, withdrawn=[W1]) == 0
    finally:
        conn.close()


def test_after_a_snapshot_a_few_missing_icons_are_left_to_lazy_fetch(tmp_path, server):
    pub = pf.build_pub(tmp_path / "p")
    pf.serve(pub, server)
    conn = pf.client_db_at(tmp_path / "c.sqlite", None)
    try:
        from ul_house.sync import service

        cache = icons.IconCache(tmp_path / "icons")
        result = pf.run_sync(conn, pf.make_client(server), pub, tmp_path, cache=cache)
        assert result.outcome is service.Outcome.SNAPSHOT
        assert len(cache.missing(conn)) == 7 and result.icons_missing == 7          # <= 25: nothing was fetched
        assert not [h for h in server.log if h.path.startswith("/v1/icons")]
        assert cache.fetch(pf.make_client(server), "equipment", W1) is not None      # lazy, on view
        assert len(server.hits(f"/v1/icons/equipment/{W1}")) == 1
    finally:
        conn.close()


def test_after_a_snapshot_many_missing_icons_fetch_the_base_pack(tmp_path, server):
    pub = pf.build_pub(tmp_path / "p", extra_icons=30)
    pf.serve(pub, server)
    conn = pf.client_db_at(tmp_path / "c.sqlite", None)
    try:
        cache = icons.IconCache(tmp_path / "icons")
        result = pf.run_sync(conn, pf.make_client(server), pub, tmp_path, cache=cache)
        assert len(server.hits("/v1/icons/packs/base-4.zip")) == 1                   # over 25 missing
        assert not [h for h in server.log if h.path.startswith("/v1/icons/equipment/")]
        assert cache.missing(conn) == [] and result.icons_installed == 37 and result.icons_missing == 0
        assert len(files_under(cache.root / "equipment")) == 37
    finally:
        conn.close()


def test_a_broken_base_pack_does_not_fail_the_sync(tmp_path, server):
    pub = pf.build_pub(tmp_path / "p", extra_icons=30)
    pf.serve(pub, server)
    server.route("/v1/icons/packs/base-4.zip", Reply(200, b"junk", {"Content-Type": "application/zip", "X-API-Commit": "dev"}))
    conn = pf.client_db_at(tmp_path / "c.sqlite", None)
    try:
        from ul_house.sync import service

        cache = icons.IconCache(tmp_path / "icons")
        result = pf.run_sync(conn, pf.make_client(server), pub, tmp_path, cache=cache)
        assert result.outcome is service.Outcome.SNAPSHOT                            # icons never fail a sync
        assert result.icons_installed == 0 and len(cache.missing(conn)) == 37
    finally:
        conn.close()
