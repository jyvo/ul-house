"""syncapi.storage (LocalPubStore) and syncapi.storage_r2 (R2PubStore over FakeR2Binding)"""
from __future__ import annotations

import asyncio
import builtins
import hashlib
import os
import shutil
from pathlib import Path

import pytest

from api_fake_binding import FakeR2Binding, JsNull
from syncapi.routing import NotAllowed, is_allowed_key
from syncapi.storage import LocalPubStore, guard
from syncapi.storage_r2 import R2PubStore

SHA = "a" * 64


def run(coro):
    return asyncio.run(coro)


async def collect(obj) -> bytes:
    out = bytearray()
    async for chunk in obj.chunks():
        out += chunk
    return bytes(out)


KEYS_REJECTED = [
    "", "current.json", "/current.json", "../build/seed/current.sqlite", "build/seed/current.sqlite", "build/history/checkpoint.json",
    "candidates/3/manifests/3.json", "secret.txt", "pub/", "pub", "pub/manifests", "pub/icons/packs", "pub/../build/seed/current.sqlite",
    "pub/../secret.txt", "pub/manifests/../../build/seed/current.sqlite", "pub/current.json\n", "pub/current.json\x00", "pub//current.json",
    "pub/CURRENT.JSON", "pub/manifests/02.json", "pub/manifests/2.json/", "pub/%2e%2e/secret.txt", "pub\\current.json", "pub/manifests\\2.json",
    f"pub/icons/packs/{SHA}.png", f"pub/icons/skill/{SHA}.png", "pub/build/seed/current.sqlite", "pub/secret.txt", "/pub/current.json",
]


@pytest.fixture
def local(store):
    return LocalPubStore(store.root, chunk_size=7)


@pytest.fixture
def store_copy(store, tmp_path):
    root = tmp_path / "store"
    shutil.copytree(store.root, root)
    return root


# local pub store
@pytest.mark.parametrize("relative", ["current.json", "manifests/2.json", "snapshots/unison-2.sqlite.gz", "updates/1-2.json.gz", "icons/packs/base-2.zip", "icons/packs/delta-1-2.zip", "lineage/2.json.gz"])
def test_local_hit(store, local, relative):
    data = (store.root / "pub" / relative).read_bytes()

    async def scenario():
        obj = await local.get(f"pub/{relative}")
        assert obj is not None and obj.not_modified is False
        assert obj.size == len(data)
        assert obj.etag == '"' + hashlib.sha256(data).hexdigest() + '"'
        return await collect(obj)

    assert run(scenario()) == data


def test_local_read_returns_the_whole_file(store, local):
    async def scenario():
        obj = await local.get("pub/current.json")
        body = await obj.read()
        await obj.close()
        return body

    assert run(scenario()) == (store.root / "pub/current.json").read_bytes()


def test_local_icon_hit(store, local):
    key = f"pub/icons/equipment/{store.icon_sha256}.png"
    obj_data = (store.root / key).read_bytes()
    assert run(_read(local, key)) == obj_data


async def _read(store, key):
    obj = await store.get(key)
    return await collect(obj)


def test_local_chunks_are_bounded_by_chunk_size(store):
    small = LocalPubStore(store.root, chunk_size=5)

    async def scenario():
        obj = await small.get("pub/manifests/2.json")
        return [len(c) async for c in obj.chunks()]

    sizes = run(scenario())
    assert len(sizes) > 1 and max(sizes) <= 5


@pytest.mark.parametrize("key", ["pub/manifests/99.json", "pub/updates/7-9.json.gz", "pub/snapshots/unison-99.sqlite.gz", f"pub/icons/equipment/{SHA}.png", "pub/lineage/99.json.gz", "pub/icons/packs/base-99.zip"])
def test_local_miss_is_none(local, key):
    assert run(local.get(key)) is None


@pytest.mark.parametrize("key", ["pub/manifests", "pub/icons/packs", "pub/icons", "pub/", "pub"])
def test_directory_key_is_not_allowed(local, key):
    with pytest.raises(NotAllowed):
        run(local.get(key))


def test_if_none_match_hit_is_not_modified(store, local):
    etag = '"' + hashlib.sha256((store.root / "pub/current.json").read_bytes()).hexdigest() + '"'

    async def scenario():
        hit = await local.get("pub/current.json", if_none_match=etag)
        miss = await local.get("pub/current.json", if_none_match='"nope"')
        star = await local.get("pub/current.json", if_none_match="*")
        out = (hit.not_modified, hit.etag, miss.not_modified, star.not_modified, await collect(miss))
        await miss.close()
        return out

    not_modified, hit_etag, miss_not_modified, star, body = run(scenario())
    assert not_modified is True and hit_etag == etag
    assert miss_not_modified is False and star is True
    assert body == (store.root / "pub/current.json").read_bytes()


def test_close_is_idempotent(local):
    async def scenario():
        obj = await local.get("pub/current.json")
        await obj.close()
        await obj.close()

    run(scenario())


def test_etag_follows_the_content(store_copy):
    local = LocalPubStore(store_copy)
    path = store_copy / "pub/current.json"

    async def etag():
        obj = await local.get("pub/current.json")
        await obj.close()
        return obj.etag

    before = run(etag())
    assert run(etag()) == before
    path.write_bytes(path.read_bytes() + b" ")
    assert run(etag()) != before


# guard before I/O
@pytest.mark.parametrize("key", KEYS_REJECTED, ids=[repr(k) for k in KEYS_REJECTED])
def test_guard_rejects(key):
    with pytest.raises(NotAllowed):
        guard(key)
    assert not is_allowed_key(key)


@pytest.mark.parametrize("key", KEYS_REJECTED, ids=[repr(k) for k in KEYS_REJECTED])
def test_local_get_rejects_before_touching_the_filesystem(store, key, monkeypatch):
    local = LocalPubStore(store.root)

    def boom(*args, **kwargs):
        raise AssertionError("filesystem touched before the allow-list check")

    async def scenario():
        for owner, name in [
            (builtins, "open"), (os, "open"), (os, "stat"), (os, "lstat"), (os, "fstat"), (os, "scandir"), (os, "listdir"),
            (Path, "open"), (Path, "stat"), (Path, "is_file"), (Path, "exists"), (Path, "resolve"), (Path, "read_bytes"),
        ]:
            monkeypatch.setattr(owner, name, boom)
        await local.get(key)

    with pytest.raises(NotAllowed):
        run(scenario())


def test_guard_returns_an_allowed_key_unchanged():
    assert guard("pub/current.json") == "pub/current.json"


# symlink tests + replacements
def test_symlink_leaving_pub_is_a_miss(store_copy):
    link = store_copy / "pub/manifests/9.json"
    link.symlink_to(store_copy / "build/seed/current.sqlite")
    local = LocalPubStore(store_copy)
    assert run(local.get("pub/manifests/9.json")) is None


def test_symlink_to_another_decoy_via_relative_path_is_a_miss(store_copy):
    link = store_copy / "pub/lineage/9.json.gz"
    link.symlink_to(Path("..") / ".." / "secret.txt")
    assert run(LocalPubStore(store_copy).get("pub/lineage/9.json.gz")) is None


def test_symlink_staying_inside_pub_is_served(store_copy):
    link = store_copy / "pub/manifests/9.json"
    link.symlink_to(store_copy / "pub/manifests/2.json")
    data = (store_copy / "pub/manifests/2.json").read_bytes()
    assert run(_read(LocalPubStore(store_copy), "pub/manifests/9.json")) == data


def test_replacing_the_file_mid_request_keeps_streaming_the_old_bytes(store_copy):
    local = LocalPubStore(store_copy, chunk_size=4)
    path = store_copy / "pub/current.json"
    old = path.read_bytes()

    async def scenario():
        obj = await local.get("pub/current.json")
        replacement = store_copy / "pub" / "current.json.new"
        replacement.write_bytes(b'{"replaced": true}')
        os.replace(replacement, path)
        return await collect(obj)

    assert run(scenario()) == old
    assert path.read_bytes() == b'{"replaced": true}'


# r2 pub store > fake binding
@pytest.fixture
def binding(store):
    return FakeR2Binding(store.root, chunk_size=5)


@pytest.mark.parametrize("relative", ["current.json", "manifests/2.json", "snapshots/unison-2.sqlite.gz", "icons/packs/base-2.zip", "lineage/2.json.gz"])
def test_r2_hit_streams_and_reads(store, binding, relative):
    data = (store.root / "pub" / relative).read_bytes()
    r2 = R2PubStore(binding)

    async def scenario():
        streamed = await r2.get(f"pub/{relative}")
        assert streamed.size == len(data) and streamed.not_modified is False
        assert streamed.etag == '"' + hashlib.md5(data).hexdigest() + '"'
        whole = await r2.get(f"pub/{relative}")
        return await collect(streamed), await whole.read()

    chunked, whole = run(scenario())
    assert chunked == whole == data


def test_r2_chunks_come_from_several_reads(store, binding):
    async def scenario():
        obj = await R2PubStore(binding).get("pub/manifests/2.json")
        return [len(c) async for c in obj.chunks()]

    sizes = run(scenario())
    assert len(sizes) > 1 and max(sizes) <= 5


def test_r2_miss_via_none(binding):
    assert run(R2PubStore(binding).get("pub/manifests/99.json")) is None


def test_r2_miss_via_jsnull_stand_in(store):
    binding = FakeR2Binding(store.root, missing_sentinel=True)
    assert isinstance(run(binding.get("pub/manifests/99.json")), JsNull)
    assert run(R2PubStore(binding).get("pub/manifests/99.json")) is None


def test_r2_if_none_match(store, binding):
    data = (store.root / "pub/current.json").read_bytes()
    etag = '"' + hashlib.md5(data).hexdigest() + '"'

    async def scenario():
        hit = await R2PubStore(binding).get("pub/current.json", if_none_match=etag)
        miss = await R2PubStore(binding).get("pub/current.json", if_none_match='"zzz"')
        weak = await R2PubStore(binding).get("pub/current.json", if_none_match=f"W/{etag}")
        return hit.not_modified, hit.etag, miss.not_modified, weak.not_modified, await collect(miss)

    hit, hit_etag, miss, weak, body = run(scenario())
    assert (hit, hit_etag, miss, weak) == (True, etag, False, True)
    assert body == data


def test_r2_close_is_idempotent(binding):
    async def scenario():
        obj = await R2PubStore(binding).get("pub/current.json")
        await obj.close()
        await obj.close()

    run(scenario())


@pytest.mark.parametrize("key", KEYS_REJECTED, ids=[repr(k) for k in KEYS_REJECTED])
def test_r2_guard_rejects_before_the_binding_is_asked(binding, key):
    with pytest.raises(NotAllowed):
        run(R2PubStore(binding).get(key))
    assert binding.requested == []


def test_r2_requested_keys_are_only_allow_listed(store, binding):
    r2 = R2PubStore(binding)

    async def scenario():
        for relative in store.objects:
            await r2.get(f"pub/{relative}")
        for key in KEYS_REJECTED:
            try:
                await r2.get(key)
            except NotAllowed:
                pass

    run(scenario())
    assert len(binding.requested) == len(store.objects)
    assert all(is_allowed_key(key) for key in binding.requested)
