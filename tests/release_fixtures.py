from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import shutil
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from ul_house import ids
from ul_house.database import entities as E

REPO = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPO / "api" / "contract" / "schemas"
SEED_SCHEMA = REPO / "src" / "ul_house" / "seed" / "schema.sql"

COMMIT = "c" * 40
OTHER_COMMIT = "4be1c2a" + "0" * 33
SHA_A = "a" * 64
CATALOG_VERSION = "ab" * 32
POLICY_VERSION = "cd" * 32
SHIPPED = E.GAME_TABLES + E.LEDGER_TABLES
SUBTYPES = ("weapon", "defensive_gear", "monster")


def utc_text(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def png(token: str) -> bytes:
    """fake PNG (hashed)"""
    return b"\x89PNG\r\n\x1a\n" + token.encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# fake s3 and failure injecting store
class FakeS3Error(Exception):
    """botocore ClientError stand-in"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeS3Client:
    """in-memory boto3 S3 stand-in"""

    def __init__(self, max_keys: int = 1000):
        self.max_keys = max_keys
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def put_object(self, *, Bucket, Key, Body, ContentType=None, Metadata=None, **_):
        data = Body if isinstance(Body, (bytes, bytearray)) else Body.read()
        self.calls.append(("put_object", Key))
        self.objects[(Bucket, Key)] = {"body": bytes(data), "meta": dict(Metadata or {}), "type": ContentType}
        return {"ETag": '"x"'}

    def get_object(self, *, Bucket, Key, **_):
        self.calls.append(("get_object", Key))
        item = self.objects.get((Bucket, Key))
        if item is None:
            raise FakeS3Error("NoSuchKey", Key)
        return {"Body": io.BytesIO(item["body"]), "ContentLength": len(item["body"]),
                "Metadata": dict(item["meta"]), "ContentType": item["type"]}

    def head_object(self, *, Bucket, Key, **_):
        self.calls.append(("head_object", Key))
        item = self.objects.get((Bucket, Key))
        if item is None:
            raise FakeS3Error("404", Key)
        return {"ContentLength": len(item["body"]), "Metadata": dict(item["meta"]),
                "ContentType": item["type"]}

    def list_objects_v2(self, *, Bucket, Prefix="", ContinuationToken=None, MaxKeys=None, **_):
        self.calls.append(("list_objects_v2", Prefix))
        keys = sorted(k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix))
        if ContinuationToken:
            keys = [k for k in keys if k > ContinuationToken]
        limit = min(MaxKeys or self.max_keys, self.max_keys)
        page, rest = keys[:limit], keys[limit:]
        out: dict[str, Any] = {
            "Contents": [{"Key": k, "Size": len(self.objects[(Bucket, k)]["body"])} for k in page],
            "KeyCount": len(page),
            "IsTruncated": bool(rest),
        }
        if rest:
            out["NextContinuationToken"] = page[-1]
        return out

    def delete_object(self, *, Bucket, Key, **_):
        self.calls.append(("delete_object", Key))
        self.objects.pop((Bucket, Key), None)
        return {}


class FlakyStore:
    """store wrapper with logging and failures"""

    def __init__(self, inner, *, fail_on: Callable[[str, str, int], bool] | None = None,
                 corrupt_get: Mapping[str, int] | Iterable[str] = (),
                 after: Callable[[str, str], None] | None = None):
        self._inner = inner
        self.fail_on = fail_on
        self.corrupt_get = dict(corrupt_get) if isinstance(corrupt_get, Mapping) else {k: 1 for k in corrupt_get}
        self.after = after
        self.log: list[tuple[str, str]] = []
        self._count: dict[str, int] = defaultdict(int)
        self._reads: dict[str, int] = defaultdict(int)

    @property
    def kind(self) -> str:
        return self._inner.kind

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def puts(self) -> list[str]:
        return [k for op, k in self.log if op == "put"]

    def _enter(self, op: str, key: str) -> None:
        from ul_house.build.store import StoreError
        self.log.append((op, key))
        index = self._count[op]
        self._count[op] += 1
        if self.fail_on is not None and self.fail_on(op, key, index):
            raise StoreError("injected")

    def _done(self, op: str, key: str) -> None:
        if self.after is not None:
            self.after(op, key)

    def put(self, key, data, *, content_type=None):
        self._enter("put", key)
        info = self._inner.put(key, data, content_type=content_type)
        self._done("put", key)
        return info

    def put_file(self, key, path, *, content_type=None):
        self._enter("put", key)
        info = self._inner.put_file(key, path, content_type=content_type)
        self._done("put", key)
        return info

    def _corrupt(self, key: str) -> bool:
        self._reads[key] += 1
        return key in self.corrupt_get and self._reads[key] >= self.corrupt_get[key]

    def get(self, key, *, sha256=None):
        from ul_house.build.store import IntegrityError, sha256_bytes
        self._enter("get", key)
        if not self._corrupt(key):
            return self._inner.get(key, sha256=sha256)
        data = self._inner.get(key)
        data = bytes([data[0] ^ 0xFF]) + data[1:] if data else b"\xff"
        if sha256 is not None and sha256_bytes(data) != sha256:
            raise IntegrityError(f"{key}: corrupted read")
        return data

    def get_file(self, key, dest, *, sha256=None):
        from ul_house.build.store import ObjectInfo, sha256_bytes
        self._enter("get", key)
        if not self._corrupt(key):
            return self._inner.get_file(key, dest, sha256=sha256)
        data = self._corrupted_bytes(key, sha256)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return ObjectInfo(key, len(data), sha256_bytes(data))

    def _corrupted_bytes(self, key, sha256):
        from ul_house.build.store import IntegrityError, sha256_bytes
        data = self._inner.get(key)
        data = bytes([data[0] ^ 0xFF]) + data[1:] if data else b"\xff"
        if sha256 is not None and sha256_bytes(data) != sha256:
            raise IntegrityError(f"{key}: corrupted read")
        return data

    def head(self, key):
        self._enter("head", key)
        return self._inner.head(key)

    def list(self, prefix=""):
        self._enter("list", prefix)
        return self._inner.list(prefix)

    def delete(self, key):
        self._enter("delete", key)
        self._inner.delete(key)
        self._done("delete", key)


# schema derived DDL
def _schema_columns() -> dict[str, list[tuple[str, str, bool, int]]]:
    """table columns read from schema"""
    sections = E.schema_sections()
    conn = sqlite3.connect(":memory:")
    conn.executescript(sections["game"] + sections["ledger"])
    out = {}
    for table in SHIPPED:
        out[table] = [(r[1], r[2].upper(), bool(r[3]), r[5])
                      for r in conn.execute(f'PRAGMA table_info("{table}")')]
    conn.close()
    return out


COLUMNS = _schema_columns()
PRIMARY_KEYS = {t: [c[0] for c in sorted((c for c in cols if c[3]), key=lambda c: c[3])]
                for t, cols in COLUMNS.items()}
_DUCK_TYPE = {"TEXT": "VARCHAR", "INTEGER": "BIGINT"}


def duck_ddl(table: str) -> str:
    """DuckDB DDL of shipped table"""
    parts = [f'"{n}" {_DUCK_TYPE[t]}{" NOT NULL" if nn else ""}' for n, t, nn, _ in COLUMNS[table]]
    return f'CREATE TABLE "{table}" (' + ", ".join(parts) + ")"


# history DDL (trimmed)
HISTORY_DDL = """
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
CREATE TABLE IF NOT EXISTS transform_run (
  run_id VARCHAR PRIMARY KEY, code_commit VARCHAR NOT NULL, parser_version BIGINT NOT NULL,
  catalog_version VARCHAR NOT NULL, catalog_commit VARCHAR NOT NULL, policy_version VARCHAR NOT NULL,
  dbt_invocation_id VARCHAR, dbt_manifest_sha256 VARCHAR, started_at VARCHAR NOT NULL,
  ended_at VARCHAR NOT NULL,
  revision BIGINT NOT NULL, bootstrap BOOLEAN NOT NULL, policy_commit VARCHAR NOT NULL,
  dirty BOOLEAN NOT NULL, crawl_run_id BIGINT NOT NULL, seed_sha256 VARCHAR NOT NULL,
  dbt_success BOOLEAN NOT NULL, prev_release_revision BIGINT, prev_release_run_id VARCHAR);
CREATE TABLE IF NOT EXISTS source_lineage (
  uid VARCHAR, source_url VARCHAR, source_hash VARCHAR, etag VARCHAR, retrieved_at VARCHAR,
  crawl_run_id VARCHAR, keep_reason VARCHAR, entry_kind VARCHAR, depth BIGINT, parent_id VARCHAR,
  dbt_valid_from VARCHAR, dbt_valid_to VARCHAR);
CREATE TABLE IF NOT EXISTS obs_data_quality (
  run_id VARCHAR NOT NULL, metric VARCHAR NOT NULL, scope VARCHAR NOT NULL, value DOUBLE,
  prev_value DOUBLE, delta_pct DOUBLE, observed_at VARCHAR NOT NULL,
  PRIMARY KEY (run_id, metric, scope));
"""

# absent in first release
RELEASE_DDL = """
CREATE TABLE IF NOT EXISTS release (
  revision BIGINT PRIMARY KEY, transform_run_id VARCHAR NOT NULL, crawl_run_id VARCHAR,
  seed_sha256 VARCHAR NOT NULL, snapshot_sha256 VARCHAR NOT NULL, package_shas VARCHAR NOT NULL,
  icon_pack_shas VARCHAR NOT NULL, manifest_sha256 VARCHAR NOT NULL, signature VARCHAR NOT NULL,
  published_at VARCHAR NOT NULL, minimum_revision BIGINT NOT NULL, snapshot_only BOOLEAN NOT NULL,
  gate_level VARCHAR NOT NULL, run_id VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS obs_approval (
  revision BIGINT PRIMARY KEY, candidate_run_id VARCHAR NOT NULL, approved_by VARCHAR NOT NULL,
  reason VARCHAR NOT NULL, approved_at VARCHAR NOT NULL, approval_run_id VARCHAR NOT NULL,
  observed_at VARCHAR NOT NULL);
"""


_NO_PK = re.compile(r",\s*PRIMARY KEY \([^)]*\)|\s+PRIMARY KEY")


def new_history(path: Path, *, release_tables: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    try:
        conn.execute(_NO_PK.sub("", HISTORY_DDL))
        if release_tables:
            conn.execute(_NO_PK.sub("", RELEASE_DDL))
    finally:
        conn.close()
    return path


def history_rows(path: Path, sql: str, params: Sequence = ()) -> list[tuple]:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return conn.execute(sql, list(params)).fetchall()
    finally:
        conn.close()


def read_json_gz(path: Path | bytes) -> Any:
    raw = path if isinstance(path, bytes) else Path(path).read_bytes()
    return json.loads(gzip.decompress(raw))


def gunzip_to(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(gzip.decompress(Path(src).read_bytes()))
    return dest


def ts(n: int) -> str:
    """wall-clock time of revision run"""
    return utc_text(datetime(2026, 10, 1, 23, 0, 0, tzinfo=UTC) + timedelta(days=n))


def generated_at(n: int) -> str:
    return utc_text(datetime(2026, 10, 1, 23, 2, 0, tzinfo=UTC) + timedelta(days=n))


class World:
    """scripted multi-revision dataset"""

    def __init__(self, tmp_path: Path, store=None, window: int = 3, *, validate: bool = True,
                 release_tables: bool = True):
        from ul_house.build.store import LocalStore
        self.root = Path(tmp_path)
        self.work = self.root / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.store = store if store is not None else LocalStore(self.root / "store")
        self.window = window
        self.contract_dir: Path | None = CONTRACT_DIR if (validate and CONTRACT_DIR.is_dir()) else None
        self.release_tables = release_tables
        self.dataset_schema_version = E.DATASET_SCHEMA_VERSION
        self.catalog_version = CATALOG_VERSION
        self.policy_version = POLICY_VERSION
        self.gate_level = "pass"
        self.gate_findings: list[dict] = []
        self.now = datetime(2026, 10, 5, 23, 21, 4, tzinfo=UTC)
        self.revision = 0
        self.rows: dict[str, dict[tuple, dict]] = {t: {} for t in SHIPPED}
        self.pending: dict[tuple[str, str], str] = {}
        self.assets: dict[str, tuple[str, bytes, str]] = {}         # (kind, body, item uid)
        self.page_changed: dict[str, str] = {}                      # uid -> last changed_at
        self._touch_pages: set[str] = set()
        self._stamp_equipment: set[str] = set()
        self._retire_stamp: set[str] = set()
        self._injected: list[tuple[str, str, str]] = []
        self.parse_failures: dict[str, dict[str, Any]] | None = {}  # empty table always exported
        self.parse_failure_schema = "build"
        self.hidden: set[str] = set()                               # uids whose aggregate the mart lacks
        self.hide_shared = True                                     # shared rows only those uids use
        self._runs: list[int] = []
        self._pem: bytes | None = None
        self._signer = None
        for element in ("fire", "water", "none"):
            self.upsert_shared("element", element, {})

    def clock(self) -> datetime:
        return self.now

    @property
    def signer(self):
        if self._signer is None:
            from ul_house.build import sign
            self._pem, _ = sign.generate_keypair()
            self._signer = sign.Signer.from_pem(self._pem)
        return self._signer

    @property
    def trusted_keys(self) -> tuple[str, ...]:
        return (self.signer.public_key_hex(),)

    def build_info(self, *, dirty: bool = False, run_id: str = "run-1"):
        from ul_house.build.release import BuildInfo
        return BuildInfo(build_commit=COMMIT, dirty=dirty, run_id=run_id,
                         runtime={"python": "3.12.7", "duckdb": duckdb.__version__, "dbt_core": "1.12.5",
                                  "dbt_duckdb": "1.11.0", "sqlite": sqlite3.sqlite_version,
                                  "cryptography": "50.0.0"})

    # change recording
    def _touch(self, entity_type: str, key: str, op: str) -> None:
        prev = self.pending.get((entity_type, key))
        k = (entity_type, key)
        if prev is None:
            self.pending[k] = op
        elif op == "DELETE":
            if prev == "INSERT":
                del self.pending[k]
            else:
                self.pending[k] = "DELETE"
        elif prev == "INSERT" and op in ("UPDATE", "RETIRE"):
            pass
        elif prev == "UPDATE" and op in ("UPDATE", "RETIRE"):
            self.pending[k] = op
        elif prev == "RETIRE" and op == "UPDATE":
            self.pending[k] = "UPDATE"
        elif prev == "DELETE" and op == "INSERT":
            self.pending[k] = "UPDATE"
        else:
            raise ValueError(f"{entity_type} {key}: {prev} then {op} in one revision")

    def inject_change(self, entity_type: str, key: str, op: str) -> None:
        """raw change row for next commit"""
        self._injected.append((entity_type, key, op))

    def _set(self, table: str, row: Mapping[str, Any]) -> dict:
        cols = [c[0] for c in COLUMNS[table]]
        unknown = set(row) - set(cols)
        assert not unknown, f"{table}: unknown columns {sorted(unknown)}"
        full = {c: row.get(c) for c in cols}
        self.rows[table][tuple(full[c] for c in PRIMARY_KEYS[table])] = full
        return full

    def _remove(self, table: str, **match: Any) -> None:
        for pk, row in list(self.rows[table].items()):
            if all(row[c] == v for c, v in match.items()):
                del self.rows[table][pk]

    def _equipment(self, uid: str) -> dict | None:
        return self.rows["equipment"].get((uid,))

    def upsert_equipment(self, uid: str, *, name: str | None = None, rarity: str = "SR",
                         gear_type: str = "sword", cost: int = 10, element: str = "fire",
                         max_level: int = 50, icon: bytes | None = None, kind: str | None = "weapon",
                         stats: Sequence[tuple[str, str, int]] = (("atk", "initial", 1000),),
                         skills: Sequence[tuple[str, int | None]] = (),
                         edges: Sequence[tuple[str, str, str]] = (), entry_kind: str = "catalog",
                         proc_id: int | None = None, ability_uid: str | None = None,
                         passive_id: int | None = None,
                         potentials: Sequence[tuple[int, int]] = ()) -> None:
        """create/rewrite an equipment"""
        existing = self._equipment(uid)
        op = "UPDATE" if existing else "INSERT"
        row = dict(existing or {})
        row.update(uid=uid, name=name or (existing or {}).get("name") or f"Gear {uid}", rarity=rarity,
                   gear_type=gear_type, cost=cost, element_id=element, max_level=max_level,
                   entry_kind=entry_kind, keep_reason="cost_band", evo_depth=0,
                   state=(existing or {}).get("state", "live"),
                   retired_revision=(existing or {}).get("retired_revision"),
                   first_seen=(existing or {}).get("first_seen", ts(self.revision + 1)),
                   last_seen=ts(self.revision + 1),
                   last_changed_revision=(existing or {}).get("last_changed_revision", 0))
        if icon is not None:
            digest = sha256(icon)
            row["icon_sha"] = digest
            if (digest,) not in self.rows["icon"]:
                self.upsert_shared("icon", digest, {"kind": "equipment", "bytes": len(icon)})
            self.assets[digest] = ("equipment", icon, uid)
        else:
            row.setdefault("icon_sha", None)
        self._set("equipment", row)
        # aggregate child rows rewritten
        self._remove_skill_links(uid)
        for table in ("stat", *SUBTYPES, "monster_skill", "potential_level"):
            self._remove(table, uid=uid)
        for label, tier, value in stats:
            self._set("stat", {"uid": uid, "label": label, "tier": tier, "value": value})
        if kind == "weapon":
            self._set("weapon", {"uid": uid, "infusion_count": 0, "proc_id": proc_id, "ability_uid": ability_uid})
        elif kind == "defensive_gear":
            self._set("defensive_gear", {"uid": uid, "infusion_count": 0, "proc_id": proc_id})
        elif kind == "monster":
            self._set("monster", {"uid": uid, "passive_id": passive_id, "restrictions": None})
            for level, effect_id in potentials:
                assert (effect_id,) in self.rows["skill_effect"], "create the skill_effect first"
                self._set("potential_level", {"uid": uid, "level": level, "effect_id": effect_id})
                self._set("effect_link", {"owner_kind": "potential_level", "owner_id": f"{uid}:{level}",
                                          "ordinal": 1, "effect_id": effect_id})
            for n, (skill_name, effect_id) in enumerate(skills, start=1):
                skill_id = ids.skill_id(uid, n)
                self._set("monster_skill", {"skill_id": skill_id, "uid": uid, "ordinal": n,
                                            "name": skill_name})
                if effect_id is not None:
                    assert (effect_id,) in self.rows["skill_effect"], "create the skill_effect first"
                    self._set("effect_link", {"owner_kind": "monster_skill", "owner_id": ids.id_to_text(skill_id),
                                              "ordinal": 1, "effect_id": effect_id})
        self._touch("equipment", uid, op)
        self._stamp_equipment.add(uid)
        self._touch_pages.add(uid)
        for edge_kind, from_uid, to_uid in edges:
            self.add_edge(edge_kind, from_uid, to_uid)

    def _remove_skill_links(self, uid: str) -> None:
        skill_ids = {ids.id_to_text(r["skill_id"]) for r in self.rows["monster_skill"].values() if r["uid"] == uid}
        potential_ids = {f'{uid}:{r["level"]}' for r in self.rows["potential_level"].values() if r["uid"] == uid}
        for pk, link in list(self.rows["effect_link"].items()):
            if (link["owner_kind"] == "monster_skill" and link["owner_id"] in skill_ids) or \
                    (link["owner_kind"] == "potential_level" and link["owner_id"] in potential_ids):
                del self.rows["effect_link"][pk]

    def update_equipment(self, uid: str, **columns: Any) -> None:
        row = self._equipment(uid)
        assert row is not None, uid
        row.update(columns)
        self._touch("equipment", uid, "UPDATE")
        self._stamp_equipment.add(uid)
        self._touch_pages.add(uid)

    def skill_effect(self, target: str | None, description: str) -> int:
        effect_id = ids.effect_id(target, description)
        self.upsert_shared("skill_effect", effect_id, {"target": target, "description": description})
        return effect_id

    def proc(self, raw_name: str, family: str, *, effects: Sequence[int] = (), conditions: Sequence[str] = (),
             scalings: Sequence[str] = (), element: str = "fire") -> int:
        """proc with family, conditions, links"""
        family_id = ids.family_id(family)
        if (family_id,) not in self.rows["proc_family"]:
            self.upsert_shared("proc_family", family_id, {"name": family})
        proc_id = ids.proc_id(raw_name, "|".join(map(str, effects)))
        self.upsert_shared(
            "proc", proc_id,
            {"family_id": family_id, "element_id": element, "size": "L", "raw_name": raw_name,
             "activation_rate": "10%", "element_position": "prefix"},
            effect_link=[{"owner_kind": "proc", "owner_id": ids.id_to_text(proc_id), "ordinal": n,
                          "effect_id": e} for n, e in enumerate(effects, start=1)],
            proc_condition=[{"proc_id": proc_id, "ordinal": n, "condition": c}
                            for n, c in enumerate(conditions, start=1)],
            proc_scaling=[{"proc_id": proc_id, "ordinal": n, "scale_kind": "ability_power", "element_id": element,
                           "gear_kind": "weapons", "cap": "5XL+", "pieces_needed": 1, "description": d}
                          for n, d in enumerate(scalings, start=1)])
        return proc_id

    def passive(self, name: str, *, effects: Sequence[int] = ()) -> int:
        """passive skill with effect links"""
        effect_hash = "|".join(map(str, effects))
        passive_id = ids.passive_id(name, effect_hash)
        self.upsert_shared(
            "passive_skill", passive_id, {"name": name, "effect_hash": effect_hash},
            effect_link=[{"owner_kind": "passive_skill", "owner_id": ids.id_to_text(passive_id), "ordinal": n,
                          "effect_id": e} for n, e in enumerate(effects, start=1)])
        return passive_id

    def ability(self, uid: str, name: str, *, effects: Sequence[int] = ()) -> str:
        """weapon ability with effect links"""
        self.upsert_shared(
            "weapon_ability", uid, {"name": name},
            effect_link=[{"owner_kind": "weapon_ability", "owner_id": uid, "ordinal": n, "effect_id": e}
                         for n, e in enumerate(effects, start=1)])
        return uid

    def add_edge(self, kind: str, from_uid: str, to_uid: str) -> None:
        def label(uid):
            row = self._equipment(uid)
            return row["name"] if row else f"Name {uid}"
        self._set("evolution_edge", {"kind": kind, "from_uid": from_uid, "to_uid": to_uid,
                                     "from_name": label(from_uid), "to_name": label(to_uid)})
        self._touch_endpoints(from_uid, to_uid)

    def remove_edge(self, kind: str, from_uid: str, to_uid: str) -> None:
        self._remove("evolution_edge", kind=kind, from_uid=from_uid, to_uid=to_uid)
        self._touch_endpoints(from_uid, to_uid)

    def add_material(self, kind: str, from_uid: str, to_uid: str, ordinal: int, ref_name: str,
                     *, material_kind: str = "item", ref_uid: str | None = None, quantity: int = 1) -> None:
        self._set("evolution_material", {"kind": kind, "from_uid": from_uid, "to_uid": to_uid,
                                         "ordinal": ordinal, "material_kind": material_kind,
                                         "ref_uid": ref_uid, "ref_name": ref_name, "quantity": quantity})
        self._touch_endpoints(from_uid, to_uid)

    def _touch_endpoints(self, *uids: str) -> None:
        """edge change touches both endpoints"""
        for uid in uids:
            if self._equipment(uid) is not None and (("equipment", uid) not in self.pending):
                self._touch("equipment", uid, "UPDATE")
                self._stamp_equipment.add(uid)

    def retire(self, uid: str, reason: str = "gone") -> None:
        row = self._equipment(uid)
        assert row is not None and row["state"] == "live", uid
        row["state"] = "retired"
        row["retired_revision"] = None                  # stamped at commit
        self._touch("equipment", uid, "RETIRE")
        self._retire_stamp.add(uid)
        if (uid,) not in self.rows["uid_retired"]:      # ledger only grows
            self._set("uid_retired", {"uid": uid, "since_revision": None, "reason": reason})
            self._touch("uid_retired", uid, "INSERT")

    def revive(self, uid: str) -> None:
        row = self._equipment(uid)
        assert row is not None and row["state"] == "retired", uid
        row["state"] = "live"
        row["retired_revision"] = None
        self._touch("equipment", uid, "UPDATE")
        self._stamp_equipment.add(uid)

    def alias(self, old: str, new: str, confirmed_commit: str = OTHER_COMMIT) -> None:
        """record old-to-new uid alias"""
        self._set("uid_alias", {"old_uid": old, "new_uid": new, "since_revision": None,
                                "confirmed_commit": confirmed_commit})
        self._touch("uid_alias", old, "INSERT")
        self._touch("equipment", old, "ALIAS")

    def drop_ledger(self, table: str, key: str) -> None:
        """remove a ledger row silently"""
        self._remove(table, **{PRIMARY_KEYS[table][0]: key})

    # shared entities
    def upsert_shared(self, entity: str, key: int | str, row: Mapping[str, Any] | None = None, **children: Sequence[Mapping[str, Any]]) -> None:
        e = E.entity(entity)
        full = dict(row or {})
        full[e.key_columns[0]] = key
        existing = (key,) in self.rows[e.root_table]
        self._set(e.root_table, full)
        for child in e.children:                           # rewrite listed child tables
            if child.table in children:
                column = child.owner_columns[0]
                for pk, value in list(self.rows[child.table].items()):
                    if str(value[column]) == str(key) and (child.owner_kind in (None, value.get("owner_kind"))):
                        del self.rows[child.table][pk]
        for table, rows in children.items():
            for child_row in rows:
                self._set(table, child_row)
        self._touch(entity, E.key_to_text(e, key), "UPDATE" if existing else "INSERT")

    def delete_shared(self, entity: str, key: int | str) -> None:
        e = E.entity(entity)
        assert e.deletable, entity
        self._remove(e.root_table, **{e.key_columns[0]: key})
        for child in e.children:
            column = child.owner_columns[0]
            for pk, value in list(self.rows[child.table].items()):
                if str(value[column]) == str(key) and (child.owner_kind in (None, value.get("owner_kind"))):
                    del self.rows[child.table][pk]
        self._touch(entity, E.key_to_text(e, key), "DELETE")

    def fail_parse(self, *uids: str, schema: str = "build", hide: bool = True, error: str = "boom",
                   hide_shared: bool = True) -> None:
        """mark uids as shipped parse failures"""
        self.hide_shared = hide_shared
        if self.parse_failures is None:
            self.parse_failures = {}
        self.parse_failure_schema = schema
        for uid in uids:
            self.parse_failures[uid] = {"uid": uid, "source_hash": sha256(f"bad-{uid}".encode()),
                                        "parser_version": 7, "error": error}
            if hide:
                self.hidden.add(uid)

    def clear_parse_failures(self, *, table: bool = True) -> None:
        self.hidden.clear()
        self.parse_failures = {} if table else None

    def _references(self, uids: set[str]) -> dict[str, set]:
        """shared keys referenced by uids"""
        rows = self.rows
        out: dict[str, set] = {t: set() for t in ("icon", "item", "proc", "proc_family", "passive_skill",
                                                  "weapon_ability", "skill_effect")}
        owners: dict[str, set[str]] = {"proc": set(), "passive_skill": set(), "weapon_ability": set(),
                                       "monster_skill": set(), "potential_level": set()}
        for (uid,), row in rows["equipment"].items():
            if uid in uids and row["icon_sha"] is not None:
                out["icon"].add(row["icon_sha"])
        for r in rows["weapon"].values():
            if r["uid"] in uids:
                if r["proc_id"] is not None:
                    out["proc"].add(r["proc_id"])
                if r["ability_uid"] is not None:
                    out["weapon_ability"].add(r["ability_uid"])
        for r in rows["defensive_gear"].values():
            if r["uid"] in uids and r["proc_id"] is not None:
                out["proc"].add(r["proc_id"])
        for r in rows["monster"].values():
            if r["uid"] in uids and r["passive_id"] is not None:
                out["passive_skill"].add(r["passive_id"])
        for r in rows["monster_skill"].values():
            if r["uid"] in uids:
                owners["monster_skill"].add(ids.id_to_text(r["skill_id"]))
        for r in rows["potential_level"].values():
            if r["uid"] in uids:
                out["skill_effect"].add(r["effect_id"])
                owners["potential_level"].add(f'{r["uid"]}:{r["level"]}')
        for r in rows["evolution_material"].values():
            if (r["from_uid"] in uids or r["to_uid"] in uids) and r["material_kind"] == "item" and r["ref_uid"]:
                out["item"].add(r["ref_uid"])
        for pid in out["proc"]:
            owners["proc"].add(ids.id_to_text(pid))
            out["proc_family"].add(rows["proc"][(pid,)]["family_id"])
        owners["passive_skill"] |= {ids.id_to_text(p) for p in out["passive_skill"]}
        owners["weapon_ability"] |= out["weapon_ability"]
        for link in rows["effect_link"].values():
            if link["owner_id"] in owners[link["owner_kind"]]:
                out["skill_effect"].add(link["effect_id"])
        return out

    def _hidden_shared(self) -> dict[str, set]:
        if not (self.hidden and self.hide_shared):
            return {}
        gone = self._references(self.hidden)
        keep = self._references({u for (u,) in self.rows["equipment"]} - self.hidden)
        return {t: keys - keep[t] for t, keys in gone.items()}

    def _mart_rows(self, table: str) -> list[dict]:
        rows = list(self.rows[table].values())
        if not self.hidden:
            return rows
        shared = self._hidden_shared()
        if table in shared:
            column = E.entity(table).key_columns[0]
            return [r for r in rows if r[column] not in shared[table]]
        if table in ("proc_condition", "proc_scaling"):
            return [r for r in rows if r["proc_id"] not in shared["proc"]] if shared else rows
        if table == "effect_link" and shared:
            hidden_owner = {"proc": {ids.id_to_text(k) for k in shared["proc"]},
                            "passive_skill": {ids.id_to_text(k) for k in shared["passive_skill"]},
                            "weapon_ability": set(shared["weapon_ability"])}
            rows = [r for r in rows if r["owner_id"] not in hidden_owner.get(r["owner_kind"], ())]
        if table in ("evolution_edge", "evolution_material"):
            return [r for r in rows if r["from_uid"] not in self.hidden and r["to_uid"] not in self.hidden]
        if table == "effect_link":
            gone = {ids.id_to_text(r["skill_id"]) for r in self.rows["monster_skill"].values()
                    if r["uid"] in self.hidden}
            gone |= {f'{r["uid"]}:{r["level"]}' for r in self.rows["potential_level"].values()
                     if r["uid"] in self.hidden}
            return [r for r in rows if r["owner_kind"] not in ("monster_skill", "potential_level")
                    or r["owner_id"] not in gone]
        if table in ("equipment", "weapon", "defensive_gear", "monster", "stat", "potential_level",
                     "monster_skill", "evolution_chain"):
            return [r for r in rows if r["uid"] not in self.hidden]
        return rows

    def set_schema_version(self, v: int) -> None:
        self.dataset_schema_version = v

    def set_catalog_version(self, fingerprint: str) -> None:
        """effective from next commit"""
        self.catalog_version = fingerprint

    def gate(self, level: str, findings: Sequence[Mapping[str, Any]] = ()) -> None:
        self.gate_level = level
        self.gate_findings = [dict(f) for f in findings]

    def corrupt_asset(self, sha: str) -> None:
        kind, body, uid = self.assets[sha]
        self.assets[sha] = (kind, body + b"-corrupted", uid)

    def drop_asset(self, sha: str) -> None:
        del self.assets[sha]

    def commit(self, n: int | None = None) -> int:
        n = n if n is not None else self.revision + 1
        for uid in self._stamp_equipment | self._retire_stamp:
            row = self._equipment(uid)
            if row is None:
                continue
            row["last_changed_revision"] = n
            if uid in self._stamp_equipment:
                row["last_seen"] = ts(n)  # pure retire changes three columns
            if uid in self._retire_stamp and row["state"] == "retired":
                row["retired_revision"] = n
        for table in ("uid_retired", "uid_alias"):
            for row in self.rows[table].values():
                if row["since_revision"] is None:
                    row["since_revision"] = n
        for uid in self._touch_pages:
            self.page_changed[uid] = ts(n)
        self._stamp_equipment.clear()
        self._retire_stamp.clear()
        self._touch_pages.clear()
        changes = dict(self.pending)
        for entity_type, key, op in self._injected:
            changes[(entity_type, key)] = op
        self.pending.clear()
        self._injected.clear()
        self.revision = n
        self._runs.append(n)
        self._write_mart()
        self._write_history(n, changes)
        self._write_seed(n)
        return n

    def _write_mart(self) -> None:
        path = self.work / "mart.duckdb"
        path.unlink(missing_ok=True)
        conn = duckdb.connect(str(path))
        try:
            for table in SHIPPED:
                conn.execute(duck_ddl(table))
                cols = [c[0] for c in COLUMNS[table]]
                rows = sorted(self._mart_rows(table),
                              key=lambda r: tuple(str(r[c]) for c in PRIMARY_KEYS[table]))
                if rows:
                    conn.executemany(
                        f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in cols)})',
                        [[r[c] for c in cols] for r in rows])
            if self.parse_failures is not None:
                schema = self.parse_failure_schema
                if schema != "main":
                    conn.execute(f"CREATE SCHEMA {schema}")
                conn.execute(f"CREATE TABLE {schema}.shipped_parse_failures (uid VARCHAR NOT NULL PRIMARY KEY, "
                             "source_hash VARCHAR, parser_version BIGINT, error VARCHAR)")
                for row in self.parse_failures.values():
                    conn.execute(f"INSERT INTO {schema}.shipped_parse_failures VALUES (?,?,?,?)",
                                 [row["uid"], row["source_hash"], row["parser_version"], row["error"]])
        finally:
            conn.close()

    def _write_history(self, n: int, changes: Mapping[tuple[str, str], str]) -> None:
        path = self.work / "history" / "current.duckdb"
        if not path.exists():
            new_history(path, release_tables=self.release_tables)
        run_id = f"transform-{n}"
        conn = duckdb.connect(str(path))
        try:
            for (entity_type, key), op in sorted(changes.items()):
                row_hash = None if op == "DELETE" else sha256(f"{entity_type}:{key}:{n}:{op}".encode())
                conn.execute("INSERT INTO dataset_changes VALUES (?,?,?,?,?,?)",
                             [n, entity_type, key, op, row_hash, ts(n)])
                if op != "ALIAS":
                    conn.execute("INSERT INTO row_version VALUES (?,?,?,?)", [entity_type, key, row_hash, n])
                conn.execute("DELETE FROM entity_current WHERE entity_type = ? AND entity_key = ?",
                             [entity_type, key])
                conn.execute("INSERT INTO entity_current VALUES (?,?,?,?,?,?,?,?,?)",
                             [entity_type, key, row_hash, row_hash, n, op, None, None, ts(n)])
            conn.execute(
                "INSERT INTO transform_run VALUES (" + ",".join("?" * 19) + ")",
                [run_id, COMMIT, 7, self.catalog_version, COMMIT, self.policy_version,
                 f"dbt-{n}", sha256(f"manifest-{n}".encode()), ts(n), generated_at(n), n, n == 1, COMMIT,
                 False, n, sha256(b"seed"), True, None, None])
            conn.execute("DELETE FROM source_lineage")
            for uid, row in sorted(self.rows["equipment"].items()):
                conn.execute("INSERT INTO source_lineage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             [uid[0], f"/detail/{uid[0]}", sha256(f"html-{uid[0]}".encode()), f'"etag-{uid[0]}"',
                              ts(n), str(n), row["keep_reason"], row["entry_kind"], 0, None, ts(1), None])
        finally:
            conn.close()
        (self.work / "transform_run.json").write_text(json.dumps({"transform_run_id": run_id}))

    def _write_seed(self, n: int) -> None:
        path = self.work / "seed.sqlite"
        path.unlink(missing_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.executescript(SEED_SCHEMA.read_text())
            for run in self._runs:
                conn.execute(
                    "INSERT INTO crawl_run (run_id, trigger, started_at, ended_at, code_commit, catalog_version,"
                    " catalog_commit, status) VALUES (?, 'schedule', ?, ?, ?, ?, ?, 'complete')",
                    (run, ts(run), generated_at(run), COMMIT, self.catalog_version, COMMIT))
            for uid, changed in sorted(self.page_changed.items()):
                conn.execute(
                    "INSERT INTO page (url, kind, item_id, status, fetched_at, changed_at, run_id) "
                    "VALUES (?, 'detail', ?, 200, ?, ?, ?)", (f"/detail/{uid}", uid, changed, changed, n))
            for digest, (kind, body, uid) in sorted(self.assets.items()):
                conn.execute(
                    "INSERT INTO asset (url, item_id, kind, body, sha256, status, fetched_at, content_type, run_id)"
                    " VALUES (?, ?, ?, ?, ?, 200, ?, 'image/png', ?)",
                    (f"/images/equipicon/{uid}.png", uid, kind, body, digest, ts(n), n))
            conn.commit()
        finally:
            conn.close()

    # release/publish
    def write_gate(self) -> None:
        (self.work / "gate.json").write_text(json.dumps({
            "level": self.gate_level, "policy_version": self.policy_version, "policy_commit": COMMIT,
            "findings": self.gate_findings}))

    def inputs(self, **overrides: Any):
        from ul_house.build.release import ReleaseInputs
        kwargs: dict[str, Any] = dict(
            work_dir=self.work, revision=self.revision, store=self.store, build=self.build_info(),
            clock=self.clock, contract_dir=self.contract_dir, window=self.window,
            dataset_schema_version=self.dataset_schema_version,
            snapshot_only_ratio=5.0)            # package size rarely decides
        kwargs.update(overrides)
        return ReleaseInputs(**kwargs)

    def release(self, **overrides: Any):
        from ul_house.build import release
        self.write_gate()
        return release.build_release(self.inputs(**overrides))

    def bundle(self, decision):
        from ul_house.build.release import Bundle
        return Bundle.load(decision.bundle_dir)

    def publish(self, decision, **kwargs: Any):
        from ul_house.build import publish
        kwargs.setdefault("signer", self.signer)
        kwargs.setdefault("trusted_keys", self.trusted_keys)
        kwargs.setdefault("clock", self.clock)
        kwargs.setdefault("contract_dir", self.contract_dir)
        kwargs.setdefault("work_dir", self.work / "publish")
        store = kwargs.pop("store", self.store)
        result = publish.publish(self.bundle(decision), store, **kwargs)
        self.pull()
        return result

    def pull(self) -> None:
        """what cli pull does for history"""
        info = self.store.head("build/history/current.duckdb")
        if info is not None:
            self.store.get_file("build/history/current.duckdb", self.work / "history" / "current.duckdb")

    def cycle(self, fn: Callable[[World], None] | None = None, *, publish: bool = True, **release_kw: Any):
        if fn is not None:
            fn(self)
        self.commit()
        decision = self.release(**release_kw)
        if publish and decision.action == "publish":
            self.publish(decision)
        return decision

    def burn(self, n: int) -> None:
        """burn revision n's release"""
        path = self.work / "history" / "current.duckdb"
        conn = duckdb.connect(str(path))
        try:
            conn.execute("DELETE FROM release WHERE revision = ?", [n])
        finally:
            conn.close()
        self.store.put_file("build/history/current.duckdb", path)

    def manifest(self, decision) -> dict:
        n = decision.revision
        return json.loads((decision.bundle_dir / "pub" / "manifests" / f"{n}.json").read_bytes())

    def package(self, decision, r: int) -> dict:
        n = decision.revision
        return read_json_gz(decision.bundle_dir / "pub" / "updates" / f"{r}-{n}.json.gz")

    def lineage(self, decision) -> dict:
        return read_json_gz(decision.bundle_dir / "pub" / "lineage" / f"{decision.revision}.json.gz")

    def bundle_json(self, decision) -> dict:
        return json.loads((decision.bundle_dir / "bundle.json").read_bytes())

    def snapshot_db(self, decision, dest: Path) -> sqlite3.Connection:
        n = decision.revision
        gunzip_to(decision.bundle_dir / "pub" / "snapshots" / f"unison-{n}.sqlite.gz", dest)
        return sqlite3.connect(dest)

    def pack_members(self, path: Path) -> list[str]:
        with __import__("zipfile").ZipFile(path) as z:
            return z.namelist()

    def artifacts(self, decision) -> tuple[dict, dict[int, dict], dict[str, dict]]:
        """manifest, packages and indexes of bundle"""
        import zipfile
        manifest = self.manifest(decision)
        packages = {u["from_revision"]: self.package(decision, u["from_revision"])
                    for u in manifest["artifacts"]["updates"]}
        packs = {}
        refs = [manifest["artifacts"]["icon_packs"]["base"], *manifest["artifacts"]["icon_packs"]["deltas"]]
        for ref in refs:
            if ref is None:
                continue
            p = decision.bundle_dir / "pub" / ref["path"]
            if p.exists():
                with zipfile.ZipFile(p) as z:
                    packs[ref["path"]] = json.loads(z.read("index.json"))
        return manifest, packages, packs

    def table_counts(self) -> dict[str, int]:
        return {t: len(self.rows[t]) for t in SHIPPED}


def copy_tree(src: Path, dest: Path) -> Path:
    shutil.copytree(src, dest, dirs_exist_ok=True)
    return dest


def base_step(w: World) -> None:
    """first revision"""
    effect = w.skill_effect("self", "Raise ATK")
    w.upsert_shared("item", "9001", {"name": "Herb"})
    w.upsert_shared("item", "9002", {"name": "Moss"})
    w.upsert_equipment("1001", name="Alpha", icon=png("alpha"))
    w.upsert_equipment("1002", name="Bravo", kind="defensive_gear", gear_type="armor")
    w.upsert_equipment("1003", name="Charlie", kind="monster", skills=[("Bash", effect)])


import pytest


@pytest.fixture(autouse=True)
def cleanup_tmp(tmp_path):
    """remove DuckDB files after tests"""
    yield
    shutil.rmtree(tmp_path, ignore_errors=True)
