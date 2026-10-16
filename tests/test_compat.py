"""compatibility with supported app versions"""
import gzip
import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

import pub_fixture as pf
from ul_house.sync import icons, protocol, verify

ROOT = Path(pf.ROOT)
TESTS = Path(pf.__file__).resolve().parent


@dataclass(frozen=True)
class AppVersion:
    name: str
    ref: str | None          # none: working tree, else git ref


#: Every supported app version.
SUPPORTED_APP_VERSIONS = (
    AppVersion(name="worktree", ref=None),
)


def materialise(app: AppVersion, base: Path) -> Path:
    """src dir of a version"""
    if app.ref is None:
        return ROOT / "src"
    target = base / f"app-{app.name}"
    if not target.exists():
        target.mkdir(parents=True)
        archive = subprocess.run(["git", "archive", app.ref, "src"], cwd=ROOT, capture_output=True)
        if archive.returncode != 0:
            pytest.skip(f"git ref {app.ref!r} of app version {app.name} is not available here")
        subprocess.run(["tar", "-x", "-C", str(target)], input=archive.stdout, check=True)
    return target / "src"


CHILD = r"""
import gzip, json, sys
import pub_fixture as pf
from ul_house import database
from ul_house.database import entities
from ul_house.sync import apply, protocol, snapshot

args = json.load(open(sys.argv[1]))
conn = database.open_database(args["db"])
blob = open(args["snapshot"], "rb").read()
snapshot.install_snapshot_blob(conn, blob, sha256=pf.sha256_hex(blob), revision=args["from_revision"],
                               app_version="supported-app", tmp_dir=args["tmp"])
pf.seed_user_data(conn, inventory=[tuple(r) for r in args["inventory"]], saved=[tuple(r) for r in args["saved"]])
user_before = pf.dump(conn, pf.USER_TABLES, rowid=True)
raw = open(args["package"], "rb").read()
package = protocol.UpdatePackage.from_json(json.loads(gzip.decompress(raw)))
apply.apply_package(conn, package, package_sha256=pf.sha256_hex(raw), app_version="supported-app")
print(json.dumps({
    "game": pf.dump(conn, pf.GAME_TABLES),
    "user_before": user_before,
    "user_after": pf.dump(conn, pf.USER_TABLES, rowid=True),
    "revision": database.applied_revision(conn),
    "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
    "fk_violations": conn.execute("PRAGMA foreign_key_check").fetchall(),
    "api_schema_version": protocol.API_SCHEMA_VERSION,
    "dataset_schema_version": entities.DATASET_SCHEMA_VERSION,
}))
"""

# user rows no alias touches unchanged
INVENTORY = [(1, 1, pf.Uid.W1, "a", "b"), (2, 1, pf.Uid.W1, None, None), (3, 1, pf.Uid.M1, "stats1", "stats2"),
             (4, 2, pf.Uid.W0, None, None)]
SAVED = [(1, pf.Uid.W1), (2, pf.Uid.M1)]


def roundtrip(rows):
    return json.loads(json.dumps(rows))


def game_dump_of_snapshot(gz_bytes: bytes, tmp: Path):
    path = tmp / "expected.sqlite"
    path.write_bytes(gzip.decompress(gz_bytes))
    conn = sqlite3.connect(path)
    try:
        return roundtrip(pf.dump(conn, pf.GAME_TABLES))
    finally:
        conn.close()


def run_child(app: AppVersion, work: Path, pub_dir: Path, from_revision: int, to_revision: int, *,
              inventory=None, saved=None) -> dict:
    src = materialise(app, work)
    work.mkdir(parents=True, exist_ok=True)
    args = {"db": str(work / "client.sqlite"), "tmp": str(work / "tmp"),
            "snapshot": str(pub_dir / "snapshots" / f"unison-{from_revision}.sqlite.gz"),
            "package": str(pub_dir / "updates" / f"{from_revision}-{to_revision}.json.gz"),
            "from_revision": from_revision,
            "inventory": INVENTORY if inventory is None else inventory, "saved": SAVED if saved is None else saved}
    (work / "args.json").write_text(json.dumps(args))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(src), str(TESTS)]))
    done = subprocess.run([sys.executable, "-c", CHILD, str(work / "args.json")], env=env, cwd=work,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"{app.name}: {done.stderr[-2000:]}"
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def fixture_pub(tmp_path_factory):
    return pf.build_pub(tmp_path_factory.mktemp("compat_pub"))


def test_the_support_list_is_not_empty_and_includes_this_tree():
    assert SUPPORTED_APP_VERSIONS
    assert "worktree" in {app.name for app in SUPPORTED_APP_VERSIONS}
    assert len({app.name for app in SUPPORTED_APP_VERSIONS}) == len(SUPPORTED_APP_VERSIONS)


@pytest.mark.parametrize("r", [1, 2, 3])
@pytest.mark.parametrize("app", SUPPORTED_APP_VERSIONS, ids=lambda a: a.name)
def test_snapshot_plus_package_equals_the_newest_snapshot(app, r, fixture_pub, tmp_path):
    n = fixture_pub.revision
    out = run_child(app, tmp_path, fixture_pub.dir, r, n)
    assert out["revision"] == n
    assert out["game"] == game_dump_of_snapshot(fixture_pub.snapshot_gz(n), tmp_path)
    assert out["user_after"] == out["user_before"]
    assert out["integrity"] == "ok" and out["fk_violations"] == []
    assert out["game"] == roundtrip(pf.expected_dump(n))


@pytest.mark.parametrize("app", SUPPORTED_APP_VERSIONS, ids=lambda a: a.name)
def test_every_supported_app_understands_the_current_release_versions(app, fixture_pub, tmp_path):
    out = run_child(app, tmp_path, fixture_pub.dir, 1, fixture_pub.revision)
    manifest = fixture_pub.manifest()
    assert out["dataset_schema_version"] == manifest["dataset_schema_version"]
    assert out["api_schema_version"] >= manifest["api_schema_version"]


def test_a_release_at_n_plus_one_applies_on_top_of_n(tmp_path):
    app = SUPPORTED_APP_VERSIONS[0]
    pub = pf.build_pub(tmp_path / "pub3", revision=3)
    for r in (1, 2):
        out = run_child(app, tmp_path / f"w{r}", pub.dir, r, 3)
        assert out["revision"] == 3 and out["game"] == roundtrip(pf.expected_dump(3))


# other prod pub trees
def real_pub_dir():
    value = os.environ.get("UL_HOUSE_PUB")
    if not value:
        pytest.skip("UL_HOUSE_PUB is not set (integration: point it at T3's real pub/ output)")
    path = Path(value)
    if not (path / "current.json").exists():
        pytest.skip(f"{path} has no current.json")
    return path


def live_uids(gz_bytes: bytes, tmp: Path, name: str) -> list[str]:
    path = tmp / f"{name}.sqlite"
    path.write_bytes(gzip.decompress(gz_bytes))
    conn = sqlite3.connect(path)
    try:
        return [u for (u,) in conn.execute("SELECT uid FROM equipment WHERE state = 'live' ORDER BY uid")]
    finally:
        conn.close()


def discover_user_rows(pub_dir: Path, r: int, n: int, tmp: Path):
    before = live_uids((pub_dir / "snapshots" / f"unison-{r}.sqlite.gz").read_bytes(), tmp, f"live-{r}")
    after = set(live_uids((pub_dir / "snapshots" / f"unison-{n}.sqlite.gz").read_bytes(), tmp, f"live-{n}"))
    package = json.loads(gzip.decompress((pub_dir / "updates" / f"{r}-{n}.json.gz").read_bytes()))
    aliased = {op["key"] for op in package["ops"] if op["op"] == "ALIAS"}
    stable = [u for u in before if u in after and u not in aliased]
    touched = [op["key"] for op in package["ops"] if op["op"] == "UPSERT" and op["entity"] == "equipment"
               and op["key"] in set(stable)]
    picks = stable[:2]
    for uid in touched:
        if uid not in picks:
            picks.append(uid)
            break
    if not picks:
        return None
    uids = (picks * 4)[:5]
    inventory = [(1, 1, uids[0], "a", "b"), (2, 1, uids[1], None, None), (3, 1, uids[-1], "stats1", "stats2"),
                 (4, 2, uids[0], None, None)]
    saved = [(1, uids[0]), (2, uids[-1])]
    return inventory, saved


def table_hashes(dump: dict) -> dict[str, str]:
    import hashlib

    return {t: hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest() for t, rows in dump.items()}


@pytest.mark.parametrize("app", SUPPORTED_APP_VERSIONS, ids=lambda a: a.name)
def test_the_real_pub_tree_applies_on_every_supported_app(app, tmp_path):
    pub_dir = real_pub_dir()
    current = json.loads((pub_dir / "current.json").read_text())
    n = current["revision"]
    manifest = json.loads((pub_dir / current["manifest"]).read_text())
    froms = [u["from_revision"] for u in manifest["artifacts"]["updates"]]
    if not froms:
        pytest.skip("snapshot-only release: no package to apply")
    expected = game_dump_of_snapshot((pub_dir / manifest["artifacts"]["snapshot"]["path"]).read_bytes(), tmp_path)
    ran = 0
    for r in froms:
        if not (pub_dir / "snapshots" / f"unison-{r}.sqlite.gz").exists():
            continue
        rows = discover_user_rows(pub_dir, r, n, tmp_path)
        if rows is None:
            continue
        out = run_child(app, tmp_path / f"r{r}", pub_dir, r, n, inventory=rows[0], saved=rows[1])
        assert out["revision"] == n, r
        assert out["integrity"] == "ok" and out["fk_violations"] == [], r
        got, want = table_hashes(out["game"]), table_hashes(expected)
        assert [t for t in want if got.get(t) != want[t]] == [], f"tables differing after {r} -> {n}"
        assert out["user_after"] == out["user_before"], r
        ran += 1
    if not ran:
        pytest.skip("the pub tree holds no retained snapshot with live uids to seed")


def test_user_rows_are_discovered_from_the_tree_it_is_given(fixture_pub, tmp_path):
    inventory, saved = discover_user_rows(fixture_pub.dir, 1, fixture_pub.revision, tmp_path)
    uids = {row[2] for row in inventory} | {uid for _p, uid in saved}
    live_in_both = set(live_uids(fixture_pub.snapshot_gz(1), tmp_path, "a")) & set(live_uids(fixture_pub.snapshot_gz(), tmp_path, "b"))
    assert uids <= live_in_both and pf.Uid.G1 not in uids
    assert {row[0] for row in inventory if row[1] == 1} >= {1, 3}
    out = run_child(SUPPORTED_APP_VERSIONS[0], tmp_path / "w", fixture_pub.dir, 1, fixture_pub.revision,
                    inventory=inventory, saved=saved)
    assert out["user_after"] == out["user_before"] and out["revision"] == fixture_pub.revision


MAKE_PUB = ROOT / "api" / "tests" / "fixtures" / "make_pub.py"


@pytest.mark.skipif(not MAKE_PUB.exists(), reason="api/tests/fixtures/make_pub.py (T4) not present yet")
def test_t4_reference_pub_is_readable_by_the_client_code(tmp_path):
    done = subprocess.run([sys.executable, str(MAKE_PUB), str(tmp_path / "store")], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    pub_dir = tmp_path / "store" / "pub"
    sys.path.insert(0, str(MAKE_PUB.parent))
    try:
        import make_pub as t4
    finally:
        sys.path.remove(str(MAKE_PUB.parent))
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    public = Ed25519PrivateKey.from_private_bytes(t4.FIXTURE_SEED).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    current = verify.verify_current((pub_dir / "current.json").read_bytes(), verify.load_public_keys([public.hex()]))
    assert verify.canonical_bytes(json.loads((pub_dir / "current.json").read_text())) == t4.canonical_json(
        {k: v for k, v in json.loads((pub_dir / "current.json").read_text()).items() if k != "sig"})
    manifest_bytes = (pub_dir / current.manifest).read_bytes()
    verify.require_sha256(manifest_bytes, current.manifest_sha256, "manifest")
    manifest = protocol.Manifest.from_json(manifest_bytes)
    verify.check_supported(manifest)
    verify.check_invariants(current, manifest)
    for ref in manifest.artifacts.updates:
        raw = (pub_dir / ref.path).read_bytes()
        verify.require_sha256(raw, ref.sha256, ref.path)
        package = protocol.UpdatePackage.from_json(verify.gunzip_bounded(raw, verify.MAX_PACKAGE_BYTES))
        assert package.from_revision == ref.from_revision and package.to_revision == current.revision
    for pack in [manifest.artifacts.icon_packs.base, *manifest.artifacts.icon_packs.deltas]:
        blob = (pub_dir / pack.path).read_bytes()
        verify.require_sha256(blob, pack.sha256, pack.path)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            listing = protocol.IconPackListing.from_json(zf.read("index.json"))
        kinds = {e.sha256: e.kind for e in listing.icons}
        result = icons.IconCache(tmp_path / "icons").install_pack(blob, expected_sha256=pack.sha256, icon_kinds=kinds)
        assert len(result.installed) == pack.icon_count and result.discarded == ()


@pytest.mark.skipif(not MAKE_PUB.exists(), reason="api/tests/fixtures/make_pub.py (T4) not present yet")
def test_t4_reference_package_applies_to_a_client_database(tmp_path):
    done = subprocess.run([sys.executable, str(MAKE_PUB), str(tmp_path / "store")], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    pub_dir = tmp_path / "store" / "pub"
    raw = (pub_dir / "updates" / "1-2.json.gz").read_bytes()
    package = protocol.UpdatePackage.from_json(verify.gunzip_bounded(raw, verify.MAX_PACKAGE_BYTES))
    conn = pf.client_db_at(tmp_path / "c.sqlite", None)
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO element (element_id) VALUES ('fire'), ('none')")
        pf.insert_rows(conn, "equipment", [pf._equipment("1000003", "Old", "sword", 5, pf.icon_sha("w0"))])
        conn.execute("INSERT INTO sync_history (revision, from_revision, kind, applied_at, app_version, package_sha256) "
                     "VALUES (1, NULL, 'snapshot', '2026-10-01T00:00:00Z', 't', ?)", ("0" * 64,))
        conn.execute("COMMIT")
        from ul_house.sync import apply

        apply.apply_package(conn, package, package_sha256=pf.sha256_hex(raw), app_version="t")
        assert conn.execute("SELECT name, state FROM equipment WHERE uid = '1000001'").fetchone() == ("Fixture Blade", "live")
        assert conn.execute("SELECT state, retired_revision FROM equipment WHERE uid = '1000003'").fetchone() == ("retired", 2)
        assert conn.execute("SELECT count(*) FROM evolution_edge WHERE from_uid = '1000001'").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


CHILD_T4 = r"""
import gzip, json, sys
import pub_fixture as pf
from ul_house import database
from ul_house.sync import apply, protocol

args = json.load(open(sys.argv[1]))
conn = database.open_database(args["db"])
conn.execute("BEGIN")
conn.execute("INSERT INTO element (element_id) VALUES ('fire'), ('none')")
pf.insert_rows(conn, "equipment", [pf._equipment("1000003", "Old", "sword", 5, pf.icon_sha("w0"))])
conn.execute("INSERT INTO sync_history (revision, from_revision, kind, applied_at, app_version, package_sha256) "
             "VALUES (1, NULL, 'snapshot', '2026-10-01T00:00:00Z', 't', ?)", ("0" * 64,))
conn.execute("COMMIT")
raw = open(args["package"], "rb").read()
package = protocol.UpdatePackage.from_json(json.loads(gzip.decompress(raw)))
apply.apply_package(conn, package, package_sha256=pf.sha256_hex(raw), app_version="supported-app")
print(json.dumps({
    "revision": database.applied_revision(conn),
    "blade": conn.execute("SELECT name, state FROM equipment WHERE uid = '1000001'").fetchone(),
    "retired": conn.execute("SELECT state, retired_revision FROM equipment WHERE uid = '1000003'").fetchone(),
    "edges": conn.execute("SELECT count(*) FROM evolution_edge WHERE from_uid = '1000001'").fetchone()[0],
    "fk": conn.execute("PRAGMA foreign_key_check").fetchall(),
}))
"""


@pytest.mark.skipif(not MAKE_PUB.exists(), reason="api/tests/fixtures/make_pub.py (T4) not present yet")
@pytest.mark.parametrize("app", SUPPORTED_APP_VERSIONS, ids=lambda a: a.name)
def test_t4_reference_package_applies_on_every_supported_app(app, tmp_path):
    done = subprocess.run([sys.executable, str(MAKE_PUB), str(tmp_path / "store")], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    src = materialise(app, tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    (work / "args.json").write_text(json.dumps({"db": str(work / "client.sqlite"),
                                                "package": str(tmp_path / "store" / "pub" / "updates" / "1-2.json.gz")}))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(src), str(TESTS)]))
    ran = subprocess.run([sys.executable, "-c", CHILD_T4, str(work / "args.json")], env=env, cwd=work,
                         capture_output=True, text=True, timeout=300)
    assert ran.returncode == 0, f"{app.name}: {ran.stderr[-2000:]}"
    out = json.loads(ran.stdout.strip().splitlines()[-1])
    assert out == {"revision": 2, "blade": ["Fixture Blade", "live"], "retired": ["retired", 2], "edges": 1, "fk": []}
