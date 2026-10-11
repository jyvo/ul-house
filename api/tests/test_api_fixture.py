from __future__ import annotations

import base64
import gzip
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import make_pub
from api_invariants import check_current, check_manifest, check_package, check_pack

MAKE_PUB = Path(make_pub.__file__)

EXPECTED_PUB = {
    "current.json",
    "manifests/2.json",
    "snapshots/unison-2.sqlite.gz",
    "updates/1-2.json.gz",
    "icons/packs/base-2.zip",
    "icons/packs/delta-1-2.zip",
    "lineage/2.json.gz",
}
DECOY_FILES = {
    "build/seed/current.sqlite": b"DECOY-BUILD-SEED",
    "build/history/checkpoint.json": b"DECOY-BUILD-HISTORY",
    "candidates/3/manifests/3.json": b"DECOY-CANDIDATE",
    "secret.txt": b"DECOY-ROOT",
}


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def pub_files(root: Path) -> dict[str, bytes]:
    return tree(root / "pub")


@pytest.fixture(scope="module")
def pub(store):
    return {
        "current": json.loads((store.root / "pub/current.json").read_bytes()),
        "manifest": json.loads((store.root / "pub/manifests/2.json").read_bytes()),
        "package": json.loads(gzip.decompress((store.root / "pub/updates/1-2.json.gz").read_bytes())),
    }


def test_exact_pub_object_set(store):
    expected = EXPECTED_PUB | {f"icons/equipment/{store.icon_sha256}.png"}
    assert set(pub_files(store.root)) == expected
    assert set(store.objects) == expected
    assert store.icon_kind == "equipment" and len(store.icon_sha256) == 64


def test_decoys_exist_outside_pub_only(store):
    for relative, marker in DECOY_FILES.items():
        assert (store.root / relative).read_bytes() == marker
    assert set(store.decoy_markers) == set(DECOY_FILES.values())
    for relative, data in pub_files(store.root).items():
        assert not any(marker in data for marker in store.decoy_markers), relative


def test_objects_map_holds_the_sha256_of_every_file(store):
    for relative, digest in store.objects.items():
        assert hashlib.sha256((store.root / "pub" / relative).read_bytes()).hexdigest() == digest


def test_revision_fields(store):
    assert (store.revision, store.minimum_revision) == (2, 1)
    assert len(store.public_key) == 32


def test_two_builds_are_byte_identical(tmp_path):
    a = make_pub.build_pub(tmp_path / "a")
    b = make_pub.build_pub(tmp_path / "b")
    assert tree(tmp_path / "a") == tree(tmp_path / "b")
    assert a.objects == b.objects and a.public_key == b.public_key


def test_different_seed_changes_the_signature_only(tmp_path):
    a = make_pub.build_pub(tmp_path / "a")
    b = make_pub.build_pub(tmp_path / "b", seed=hashlib.sha256(b"another key").digest())
    assert a.public_key != b.public_key
    fa, fb = pub_files(tmp_path / "a"), pub_files(tmp_path / "b")
    assert {k for k in fa if fa[k] != fb[k]} == {"current.json"}


def test_every_artifact_validates(contract, store, pub):
    contract.validate("current", pub["current"])
    contract.validate("manifest", pub["manifest"])
    contract.validate("update_package", pub["package"])
    for name in ("base-2.zip", "delta-1-2.zip"):
        with zipfile.ZipFile(store.root / "pub/icons/packs" / name) as archive:
            contract.validate("icon_pack", json.loads(archive.read("index.json")))


def test_json_objects_are_stored_in_canonical_form(store, pub):
    assert (store.root / "pub/current.json").read_bytes() == make_pub.canonical_json(pub["current"])
    assert (store.root / "pub/manifests/2.json").read_bytes() == make_pub.canonical_json(pub["manifest"])


def test_manifest_hashes_and_sizes_match_the_files(store, pub):
    art = pub["manifest"]["artifacts"]
    refs = [art["snapshot"], art["lineage"], art["icon_packs"]["base"], *art["updates"], *art["icon_packs"]["deltas"]]
    assert len(refs) == 5
    for ref in refs:
        data = (store.root / "pub" / ref["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == ref["sha256"], ref["path"]
        assert len(data) == ref["bytes"], ref["path"]
    manifest_bytes = (store.root / "pub/manifests/2.json").read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == pub["current"]["manifest_sha256"]


def test_invariants_inv1_to_inv9_hold(store, pub):
    check_current(pub["current"])
    check_manifest(pub["manifest"], pub["current"])
    delta = zipfile.ZipFile(store.root / "pub/icons/packs/delta-1-2.zip")
    check_package(pub["package"], pub["manifest"], json.loads(delta.read("index.json")))
    for entry in [pub["manifest"]["artifacts"]["icon_packs"]["base"], *pub["manifest"]["artifacts"]["icon_packs"]["deltas"]]:
        archive = zipfile.ZipFile(store.root / "pub" / entry["path"])
        names = archive.namelist()
        members = {name: archive.read(name) for name in names}
        check_pack(json.loads(members["index.json"]), members, names, pub["manifest"], entry["path"])


def test_inv10_every_referenced_icon_exists(store, pub):
    for op in pub["package"]["ops"]:
        if op["entity"] == "icon" and op["op"] == "UPSERT":
            kind = op["data"]["icon"]["kind"]
            assert (store.root / "pub/icons" / kind / f"{op['key']}.png").is_file()


def test_icon_is_a_png_whose_name_is_its_sha256(store):
    path = store.root / f"pub/icons/equipment/{store.icon_sha256}.png"
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert hashlib.sha256(data).hexdigest() == store.icon_sha256


def test_gzip_and_zip_are_deterministic_by_construction(store):
    raw = (store.root / "pub/updates/1-2.json.gz").read_bytes()
    assert raw[:2] == b"\x1f\x8b" and raw[4:8] == b"\x00\x00\x00\x00", "gzip mtime must be 0"
    with zipfile.ZipFile(store.root / "pub/icons/packs/base-2.zip") as archive:
        assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert archive.namelist()[0] == "index.json"


def test_snapshot_and_lineage_are_gzip(store):
    assert gzip.decompress((store.root / "pub/snapshots/unison-2.sqlite.gz").read_bytes()).startswith(b"SQLite format 3\x00")
    assert json.loads(gzip.decompress((store.root / "pub/lineage/2.json.gz").read_bytes()))["revision"] == 2


def verify(public_key: bytes, doc: dict) -> None:
    body = {k: v for k, v in doc.items() if k != "sig"}
    Ed25519PublicKey.from_public_bytes(public_key).verify(base64.b64decode(doc["sig"], validate=True), make_pub.canonical_json(body))


def test_signature_verifies(store, pub):
    assert len(base64.b64decode(pub["current"]["sig"], validate=True)) == 64
    verify(store.public_key, pub["current"])


@pytest.mark.parametrize("field", ["revision", "dataset_schema_version", "api_schema_version", "minimum_revision", "manifest", "manifest_sha256", "published_at", "checked_at", "expires_at"])
def test_changing_any_field_breaks_the_signature(store, pub, field):
    doc = dict(pub["current"])
    value = doc[field]
    doc[field] = value + 1 if isinstance(value, int) else value[:-1] + ("0" if value[-1] != "0" else "1")
    with pytest.raises(InvalidSignature):
        verify(store.public_key, doc)


def test_an_added_property_breaks_the_signature(store, pub):
    with pytest.raises(InvalidSignature):
        verify(store.public_key, {**pub["current"], "extra": 1})


def test_cli_writes_the_same_tree(tmp_path, store):
    out = tmp_path / "cli"
    result = subprocess.run([sys.executable, str(MAKE_PUB), str(out)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == out
    assert tree(out) == tree(store.root)
