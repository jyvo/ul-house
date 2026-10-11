"""ref producer of every contract artifact"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import struct
import sys
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

FIXTURE_SEED: bytes = hashlib.sha256(b"ul-house api fixture signing key").digest()

REVISION = 2
MINIMUM_REVISION = 1
PUBLISHED_AT = "2026-10-04T23:21:04Z"
CHECKED_AT = "2026-10-04T23:21:04Z"
EXPIRES_AT = "2026-10-18T23:21:04Z"
FIRST_SEEN = "2026-10-03T12:00:00Z"

DECOYS: dict[str, bytes] = {
    "build/seed/current.sqlite": b"DECOY-BUILD-SEED",
    "build/history/checkpoint.json": b"DECOY-BUILD-HISTORY",
    "candidates/3/manifests/3.json": b"DECOY-CANDIDATE",
    "secret.txt": b"DECOY-ROOT",
}

ENTITY_COUNT_TABLES = (
    "equipment", "weapon", "defensive_gear", "monster", "stat",
    "skill_effect", "weapon_ability", "monster_skill", "passive_skill", "potential_level", "effect_link",
    "proc_family", "proc", "proc_condition", "proc_scaling",
    "evolution_edge", "evolution_chain", "evolution_material",
    "element", "element_relation", "item", "icon",
    "uid_alias", "uid_retired",
)  # fmt: skip


@dataclass(frozen=True)
class FixturePub:
    root: Path
    revision: int
    minimum_revision: int
    public_key: bytes
    icon_kind: str
    icon_sha256: str
    objects: dict[str, str]
    decoy_markers: tuple[bytes, ...]


def canonical_json(obj: object) -> bytes:
    """canonical form: sorted keys, compact separators, ensure_ascii (default), UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sign_pointer(doc: dict, seed: bytes = FIXTURE_SEED) -> dict:
    body = {k: v for k, v in doc.items() if k != "sig"}
    signature = Ed25519PrivateKey.from_private_bytes(seed).sign(canonical_json(body))
    return {**body, "sig": base64.b64encode(signature).decode("ascii")}


def public_key_bytes(seed: bytes = FIXTURE_SEED) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def make_png() -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    raw = b"\x00" + bytes([200, 50, 50, 255])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def make_zip(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            archive.writestr(info, data)
    return buffer.getvalue()


def make_gzip(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=9, mtime=0)


def _write(root: Path, relative: str, data: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _pack_index(name: str, pack_type: str, from_revision: int | None, icon_sha: str, png: bytes) -> bytes:
    return canonical_json(
        {
            "name": name,
            "pack_type": pack_type,
            "from_revision": from_revision,
            "to_revision": REVISION,
            "icons": [{"sha256": icon_sha, "kind": "equipment", "bytes": len(png), "path": f"equipment/{icon_sha}.png"}],
        }
    )


def build_package(icon_sha: str, png_size: int, delta_pack: str, delta_pack_sha: str) -> dict:
    equipment_row = {
        "uid": "1000001", "name": "Fixture Blade", "rarity": "SSR", "gear_type": "weapon",
        "cost": 12, "element_id": "fire", "max_level": 80, "icon_sha": icon_sha,
        "entry_kind": "catalog", "keep_reason": "cost_band", "evo_depth": 0,
        "state": "live", "retired_revision": None,
        "first_seen": FIRST_SEEN, "last_seen": CHECKED_AT, "last_changed_revision": REVISION,
    }  # fmt: skip
    return {
        "from_revision": MINIMUM_REVISION,
        "to_revision": REVISION,
        "dataset_schema_version": 1,
        "ops": [
            {
                "op": "UPSERT", "entity": "icon", "key": icon_sha, "rev": REVISION,
                "data": {"icon": {"sha256": icon_sha, "kind": "equipment", "bytes": png_size}},
            },
            {
                "op": "UPSERT", "entity": "equipment", "key": "1000001", "rev": REVISION,
                "data": {
                    "equipment": equipment_row,
                    "weapon": {"uid": "1000001", "infusion_count": 0, "proc_id": None, "ability_uid": None},
                    "stat": [
                        {"uid": "1000001", "label": "atk", "tier": "max", "value": 640},
                        {"uid": "1000001", "label": "def", "tier": "max", "value": 320},
                    ],
                    "monster_skill": [],
                    "potential_level": [],
                    "effect_link": [],
                    "evolution_edge": [
                        {
                            "kind": "reforge", "from_uid": "1000001", "to_uid": "1000002",
                            "evidence": "both", "from_name": "Fixture Blade", "to_name": "Fixture Blade+",
                        }
                    ],
                    "evolution_chain": [],
                    "evolution_material": [],
                },
            },
            {"op": "RETIRE", "entity": "equipment", "key": "1000003", "rev": REVISION, "reason": "gone"},
            {
                "op": "UPSERT", "entity": "uid_retired", "key": "1000003", "rev": REVISION,
                "data": {"uid_retired": {"uid": "1000003", "since_revision": REVISION, "reason": "gone"}},
            },
        ],
        "icons": {"pack": delta_pack, "pack_sha256": delta_pack_sha, "added": [icon_sha]},
    }  # fmt: skip


def build_pub(root: Path, *, seed: bytes = FIXTURE_SEED) -> FixturePub:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    objects: dict[str, str] = {}

    def put(relative: str, data: bytes) -> str:
        _write(root, "pub/" + relative, data)
        digest = sha256_hex(data)
        objects[relative] = digest
        return digest

    png = make_png()
    icon_sha = sha256_hex(png)
    put(f"icons/equipment/{icon_sha}.png", png)

    base_name, delta_name = f"base-{REVISION}.zip", f"delta-{MINIMUM_REVISION}-{REVISION}.zip"
    base_zip = make_zip(
        [("index.json", _pack_index(base_name, "base", None, icon_sha, png)), (f"equipment/{icon_sha}.png", png)]
    )
    delta_zip = make_zip(
        [("index.json", _pack_index(delta_name, "delta", MINIMUM_REVISION, icon_sha, png)), (f"equipment/{icon_sha}.png", png)]
    )
    base_sha = put(f"icons/packs/{base_name}", base_zip)
    delta_sha = put(f"icons/packs/{delta_name}", delta_zip)

    package = make_gzip(canonical_json(build_package(icon_sha, len(png), f"icons/packs/{delta_name}", delta_sha)))
    update_path = f"updates/{MINIMUM_REVISION}-{REVISION}.json.gz"
    update_sha = put(update_path, package)

    snapshot = make_gzip(b"SQLite format 3\x00" + b"\x00" * 100 + b"FIXTURE-SNAPSHOT")
    snapshot_path = f"snapshots/unison-{REVISION}.sqlite.gz"
    snapshot_sha = put(snapshot_path, snapshot)

    lineage = make_gzip(canonical_json({"revision": REVISION}))
    lineage_path = f"lineage/{REVISION}.json.gz"
    lineage_sha = put(lineage_path, lineage)

    counts = {table: 0 for table in ENTITY_COUNT_TABLES}
    counts.update(equipment=1, weapon=1, stat=2, evolution_edge=1, icon=1, uid_retired=1)
    manifest = {
        "revision": REVISION,
        "dataset_schema_version": 1,
        "api_schema_version": 1,
        "minimum_revision": MINIMUM_REVISION,
        "snapshot_only": False,
        "snapshot_only_reasons": [],
        "freshness": {"source_last_updated": "2026-10-04T20:00:00Z", "dataset_generated_at": "2026-10-04T23:00:00Z"},
        "build": {
            "build_commit": sha256_hex(b"build")[:40],
            "transform_run_id": "2",
            "crawl_run_id": "2",
            "seed_sha256": sha256_hex(b"seed"),
            "parser_version": 1,
            "catalog_version": sha256_hex(b"catalog"),
            "catalog_commit": sha256_hex(b"catalog-commit")[:40],
            "policy_version": sha256_hex(b"policy"),
            "policy_commit": sha256_hex(b"policy-commit")[:40],
            "runtime": {"python": "3.12.0", "duckdb": "1.1.0", "dbt_core": "1.8.0", "dbt_duckdb": "1.8.0"},
        },
        "entity_counts": counts,
        "artifacts": {
            "snapshot": {"path": snapshot_path, "sha256": snapshot_sha, "bytes": len(snapshot)},
            "updates": [
                {"from_revision": MINIMUM_REVISION, "path": update_path, "sha256": update_sha, "bytes": len(package)}
            ],
            "icon_packs": {
                "base": {"path": f"icons/packs/{base_name}", "sha256": base_sha, "bytes": len(base_zip), "icon_count": 1},
                "deltas": [
                    {
                        "from_revision": MINIMUM_REVISION,
                        "path": f"icons/packs/{delta_name}",
                        "sha256": delta_sha,
                        "bytes": len(delta_zip),
                        "icon_count": 1,
                    }
                ],
            },
            "lineage": {"path": lineage_path, "sha256": lineage_sha, "bytes": len(lineage)},
        },
        "icons": {"count": 1, "withdrawn": []},
        "gate": {"level": "pass", "warnings": []},
    }
    manifest_bytes = canonical_json(manifest)
    manifest_sha = put(f"manifests/{REVISION}.json", manifest_bytes)

    pointer = sign_pointer(
        {
            "revision": REVISION,
            "dataset_schema_version": 1,
            "api_schema_version": 1,
            "minimum_revision": MINIMUM_REVISION,
            "manifest": f"manifests/{REVISION}.json",
            "manifest_sha256": manifest_sha,
            "published_at": PUBLISHED_AT,
            "checked_at": CHECKED_AT,
            "expires_at": EXPIRES_AT,
        },
        seed,
    )
    put("current.json", canonical_json(pointer))

    for relative, data in DECOYS.items():
        _write(root, relative, data)

    return FixturePub(
        root=root,
        revision=REVISION,
        minimum_revision=MINIMUM_REVISION,
        public_key=public_key_bytes(seed),
        icon_kind="equipment",
        icon_sha256=icon_sha,
        objects=dict(sorted(objects.items())),
        decoy_markers=tuple(DECOYS.values()),
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: make_pub.py OUT_DIR", file=sys.stderr)
        return 2
    print(build_pub(Path(argv[1])).root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
