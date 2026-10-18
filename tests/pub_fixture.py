"""signed pub tree, client database"""
from __future__ import annotations

import base64
import copy
import functools
import gzip
import hashlib
import io
import json
import os
import sqlite3
import struct
import zipfile
import zlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ul_house import ids
from ul_house.database import entities

ROOT = Path(__file__).resolve().parents[1]
# contract directory, overridable
CONTRACT = Path(os.environ.get("UL_HOUSE_CONTRACT_DIR") or ROOT / "api" / "contract")
SCHEMAS = CONTRACT / "schemas"

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
PUBLISHED_AT = "2026-10-01T00:00:00Z"
CHECKED_AT = "2026-10-04T00:00:00Z"
EXPIRES_AT = "2026-10-18T00:00:00Z"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"

GAME_TABLES: tuple[str, ...] = tuple(entities.GAME_TABLES) + tuple(entities.LEDGER_TABLES)
USER_TABLES: tuple[str, ...] = tuple(entities.USER_TABLES)
# parents before children
LOAD_ORDER: tuple[str, ...] = (
    "element", "element_relation", "item", "icon", "skill_effect", "proc_family", "proc", "proc_condition",
    "proc_scaling", "weapon_ability", "passive_skill", "equipment", "weapon", "defensive_gear", "monster",
    "monster_skill", "potential_level", "effect_link", "stat", "evolution_edge", "evolution_chain",
    "evolution_material", "uid_retired", "uid_alias",
)
ONE_TO_ONE = frozenset({"weapon", "defensive_gear", "monster"})


class Uid:
    W0, W1, W2 = "1000", "1001", "1002"
    G1, G2, G3 = "2001", "2002", "2003"
    M1 = "3001"
    ABILITY = "5001"
    ITEM = "7001"
    GHOST = "9999"


# keys and sigs
@dataclass(frozen=True)
class Keypair:
    private: Ed25519PrivateKey
    public_hex: str

    def sign(self, data: bytes) -> str:
        return base64.b64encode(self.private.sign(data)).decode("ascii")


def make_keypair(name: str = "test") -> Keypair:
    """deterministic throw-away Ed25519 pair"""
    private = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(f"ul-house-test-{name}".encode()).digest())
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return Keypair(private, raw.hex())


KEY = make_keypair("one")
KEY2 = make_keypair("two")


def canonical(doc: Mapping) -> bytes:
    """shared canonical signing bytes"""
    from ul_house.sync import verify

    return verify.canonical_bytes(doc)


def sign_current(doc: dict, key: Keypair) -> dict:
    signed = {k: v for k, v in doc.items() if k != "sig"}
    signed["sig"] = key.sign(canonical(signed))
    return signed


def encode_json(doc: Mapping) -> bytes:
    return (json.dumps(doc, indent=2, sort_keys=False) + "\n").encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gz(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)


# icons
def make_png(seed: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    pixel = b"\x00" + bytes([(seed * 37) % 256, (seed * 91) % 256, (seed * 53) % 256])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", f"seed\x00{seed}".encode()) + chunk(b"IDAT", zlib.compress(pixel)) + chunk(b"IEND", b""))


# sha256 to icon bytes
ICON_BYTES: dict[str, bytes] = {}
ICON_SEEDS = {"w0": 1, "w1": 2, "w2": 3, "g1": 4, "g2": 5, "g3": 6, "m1": 7}


def _icon(seed: int) -> str:
    data = make_png(seed)
    sha = sha256_hex(data)
    ICON_BYTES[sha] = data
    return sha


def icon_sha(name: str) -> str:
    return _icon(ICON_SEEDS[name])


def _seen(rev: int) -> str:
    return f"2026-10-{rev:02d}T00:00:00Z"


def _equipment(uid: str, name: str, gear_type: str, cost: int, icon: str, *, rev: int = 1, element: str = "fire",
               evo_depth: int = 0, state: str = "live", retired_revision: int | None = None) -> dict:
    return dict(uid=uid, name=name, rarity="SR", gear_type=gear_type, cost=cost, element_id=element, max_level=80,
                icon_sha=icon, entry_kind="catalog", keep_reason="catalog", evo_depth=evo_depth, state=state,
                retired_revision=retired_revision, first_seen="2026-09-01T00:00:00Z", last_seen=_seen(rev),
                last_changed_revision=rev)


def _icon_row(sha: str) -> dict:
    return dict(sha256=sha, kind="equipment", bytes=len(ICON_BYTES[sha]))


def _effect(target: str, description: str) -> dict:
    return dict(effect_id=ids.effect_id(target, description), target=target, description=description)


EFFECT_A = _effect("self", "ATK up")
EFFECT_B = _effect("enemy", "DEF down")
EFFECT_C = _effect("self", "Heal")
EFFECT_OLD = _effect("all", "Old effect")
FAMILY = dict(family_id=ids.family_id("Blaze"), name="Blaze")
PROC = dict(proc_id=ids.proc_id("Blaze Strike", "h1"), family_id=FAMILY["family_id"], element_id="fire", size="M",
            raw_name="Blaze Strike", activation_rate="30%", element_position="prefix")
PASSIVE = dict(passive_id=ids.passive_id("Guard", "eh1"), name="Guard", effect_hash="eh1")
SKILL1 = ids.skill_id(Uid.M1, 1)
SKILL2 = ids.skill_id(Uid.M1, 2)


def _link(kind: str, owner: str, ordinal: int, effect: dict) -> dict:
    return dict(owner_kind=kind, owner_id=owner, ordinal=ordinal, effect_id=effect["effect_id"])


def _stats(uid: str, atk: int) -> list[dict]:
    return [dict(uid=uid, label="atk", tier="initial", value=atk), dict(uid=uid, label="atk", tier="max", value=atk * 2)]


def _empty() -> dict[str, list[dict]]:
    return {table: [] for table in LOAD_ORDER}


def _rev1(extra_icons: int) -> dict[str, list[dict]]:
    s = _empty()
    s["element"] = [dict(element_id="fire"), dict(element_id="none")]
    s["element_relation"] = [dict(element_id="fire", related_id="none", kind="effective")]
    s["item"] = [dict(uid=Uid.ITEM, name="Iron Ore")]
    shas = [icon_sha(n) for n in ("w0", "w1", "g1", "g3", "m1")] + [_icon(100 + i) for i in range(extra_icons)]
    s["icon"] = [_icon_row(sha) for sha in shas]
    s["skill_effect"] = [EFFECT_A, EFFECT_B, EFFECT_C, EFFECT_OLD]
    s["proc_family"] = [FAMILY]
    s["proc"] = [PROC]
    s["proc_condition"] = [dict(proc_id=PROC["proc_id"], ordinal=1, condition="HP above 50%")]
    s["proc_scaling"] = [dict(proc_id=PROC["proc_id"], ordinal=1, scale_kind="ability_power", element_id="fire",
                              gear_kind="weapons", cap="5XL+", pieces_needed=None, description="Scales with fire")]
    s["weapon_ability"] = [dict(uid=Uid.ABILITY, name="Slash")]
    s["passive_skill"] = [PASSIVE]
    s["equipment"] = [
        _equipment(Uid.W0, "Old Sword", "sword", 10, icon_sha("w0")),
        _equipment(Uid.W1, "Sword", "sword", 20, icon_sha("w1"), evo_depth=1),
        _equipment(Uid.G1, "Plate", "armor", 15, icon_sha("g1")),
        _equipment(Uid.G3, "Helm", "armor", 12, icon_sha("g3")),
        _equipment(Uid.M1, "Imp", "monster", 8, icon_sha("m1"), element="none"),
    ]
    s["weapon"] = [dict(uid=Uid.W0, infusion_count=0, proc_id=None, ability_uid=None),
                   dict(uid=Uid.W1, infusion_count=1, proc_id=PROC["proc_id"], ability_uid=Uid.ABILITY)]
    s["defensive_gear"] = [dict(uid=Uid.G1, infusion_count=0, proc_id=PROC["proc_id"]),
                           dict(uid=Uid.G3, infusion_count=0, proc_id=None)]
    s["monster"] = [dict(uid=Uid.M1, passive_id=PASSIVE["passive_id"], restrictions=None)]
    s["monster_skill"] = [dict(skill_id=SKILL1, uid=Uid.M1, ordinal=1, name="Bite"),
                          dict(skill_id=SKILL2, uid=Uid.M1, ordinal=2, name="Howl")]
    s["potential_level"] = [dict(uid=Uid.M1, level=1, effect_id=EFFECT_A["effect_id"]),
                            dict(uid=Uid.M1, level=2, effect_id=EFFECT_B["effect_id"])]
    s["effect_link"] = [
        _link("proc", ids.id_to_text(PROC["proc_id"]), 1, EFFECT_A),
        _link("weapon_ability", Uid.ABILITY, 1, EFFECT_OLD),
        _link("weapon_ability", Uid.ABILITY, 2, EFFECT_B),
        _link("passive_skill", ids.id_to_text(PASSIVE["passive_id"]), 1, EFFECT_B),
        _link("monster_skill", ids.id_to_text(SKILL1), 1, EFFECT_A),
        _link("monster_skill", ids.id_to_text(SKILL2), 1, EFFECT_B),
        _link("potential_level", f"{Uid.M1}:1", 1, EFFECT_C),
    ]
    s["stat"] = _stats(Uid.W0, 50) + _stats(Uid.W1, 100) + _stats(Uid.G1, 30) + _stats(Uid.G3, 20) + _stats(Uid.M1, 10)
    s["evolution_edge"] = [
        dict(kind="reforge", from_uid=Uid.W0, to_uid=Uid.W1, from_name="Old Sword", to_name="Sword"),
        dict(kind="awakening", from_uid=Uid.W1, to_uid=Uid.GHOST, from_name="Sword",
             to_name="Ghost Blade"),
    ]
    s["evolution_chain"] = [dict(kind="reforge", chain_id=Uid.W0, uid=Uid.W0, position=0),
                            dict(kind="reforge", chain_id=Uid.W0, uid=Uid.W1, position=1)]
    s["evolution_material"] = [
        dict(kind="reforge", from_uid=Uid.W0, to_uid=Uid.W1, ordinal=1, material_kind="item", ref_uid=Uid.ITEM,
             ref_name="Iron Ore", quantity=3),
        dict(kind="awakening", from_uid=Uid.W1, to_uid=Uid.GHOST, ordinal=1, material_kind="gear", ref_uid=None,
             ref_name="Mystery Gear", quantity=1),
    ]
    return s


def _find(state: dict, table: str, **match) -> dict:
    (row,) = [r for r in state[table] if all(r[k] == v for k, v in match.items())]
    return row


def _rev2(s: dict) -> dict:
    w1 = _find(s, "equipment", uid=Uid.W1)
    w1["last_changed_revision"] = 2
    w1["last_seen"] = _seen(2)
    s["stat"] = [r for r in s["stat"] if r["uid"] != Uid.W1] + _stats(Uid.W1, 120)
    s["icon"].append(_icon_row(icon_sha("w2")))
    s["equipment"].append(_equipment(Uid.W2, "Sword+", "sword", 25, icon_sha("w2"), rev=2, evo_depth=2))
    s["weapon"].append(dict(uid=Uid.W2, infusion_count=0, proc_id=PROC["proc_id"], ability_uid=None))
    s["stat"] += _stats(Uid.W2, 140)
    s["evolution_edge"].append(dict(kind="reforge", from_uid=Uid.W1, to_uid=Uid.W2,
                                    from_name="Sword", to_name="Sword+"))
    s["evolution_chain"].append(dict(kind="reforge", chain_id=Uid.W0, uid=Uid.W2, position=2))
    s["evolution_material"].append(dict(kind="reforge", from_uid=Uid.W1, to_uid=Uid.W2, ordinal=1,
                                        material_kind="item", ref_uid=Uid.ITEM, ref_name="Iron Ore", quantity=5))
    for uid, reason in ((Uid.G1, "gone"), (Uid.G3, "out_of_scope")):
        row = _find(s, "equipment", uid=uid)
        row.update(state="retired", retired_revision=2, last_changed_revision=2)
        s["uid_retired"].append(dict(uid=uid, since_revision=2, reason=reason))
    return s


def _rev3(s: dict) -> dict:
    s["icon"].append(_icon_row(icon_sha("g2")))
    s["equipment"].append(_equipment(Uid.G2, "Plate II", "armor", 18, icon_sha("g2"), rev=3))
    s["defensive_gear"].append(dict(uid=Uid.G2, infusion_count=0, proc_id=PROC["proc_id"]))
    s["stat"] += _stats(Uid.G2, 35)
    s["uid_alias"].append(dict(old_uid=Uid.G1, new_uid=Uid.G2, since_revision=3, confirmed_commit=COMMIT))
    s["effect_link"] = [r for r in s["effect_link"]
                        if not (r["owner_kind"] == "weapon_ability" and r["ordinal"] == 1)]
    s["effect_link"].append(_link("weapon_ability", Uid.ABILITY, 1, EFFECT_C))
    s["skill_effect"] = [r for r in s["skill_effect"] if r["effect_id"] != EFFECT_OLD["effect_id"]]
    return s


def _rev4(s: dict) -> dict:
    g3 = _find(s, "equipment", uid=Uid.G3)
    g3.update(state="live", retired_revision=None, last_changed_revision=4, last_seen=_seen(4))
    return s


@functools.cache
def make_states(extra_icons: int = 0) -> dict[int, dict[str, list[dict]]]:
    states = {1: _rev1(extra_icons)}
    states[2] = _rev2(copy.deepcopy(states[1]))
    states[3] = _rev3(copy.deepcopy(states[2]))
    states[4] = _rev4(copy.deepcopy(states[3]))
    return states


STATES = make_states(0)
LATEST = 4


def insert_rows(conn: sqlite3.Connection, table: str, rows: Iterable[Mapping]) -> None:
    for row in rows:
        cols = list(row)
        conn.execute(f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                     [row[c] for c in cols])


def load_state(conn: sqlite3.Connection, state: Mapping[str, list[dict]]) -> None:
    for table in LOAD_ORDER:
        insert_rows(conn, table, state[table])


@functools.cache
def state_db(revision: int, extra_icons: int = 0) -> sqlite3.Connection:
    """game and ledger tables, in memory"""
    sections = entities.schema_sections()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(sections["game"] + sections["ledger"])
    with conn:
        load_state(conn, make_states(extra_icons)[revision])
    return conn


def entity_counts(revision: int, extra_icons: int = 0) -> dict[str, int]:
    conn = state_db(revision, extra_icons)
    return {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in GAME_TABLES}


# comparing databases
def dump(conn: sqlite3.Connection, tables: Iterable[str] = GAME_TABLES, *, rowid: bool = False,
         schema: str = "main") -> dict[str, list[tuple]]:
    """rows sorted by every column"""
    out = {}
    for table in tables:
        cols = [r[1] for r in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]
        select = ", ".join(([f"rowid"] if rowid else []) + [f'"{c}"' for c in cols])
        order = ", ".join(str(i + 1) for i in range(len(cols) + (1 if rowid else 0)))
        out[table] = [tuple(r) for r in conn.execute(f'SELECT {select} FROM {schema}."{table}" ORDER BY {order}')]
    return out


def expected_dump(revision: int, extra_icons: int = 0) -> dict[str, list[tuple]]:
    return dump(state_db(revision, extra_icons), GAME_TABLES)


# aggregate and packages
def _sortkey(row: Mapping) -> tuple:
    return tuple((v is not None, v if v is not None else 0) for v in row.values())


def _rows(conn: sqlite3.Connection, table: str, where: str, params: Sequence) -> list[dict]:
    cur = conn.execute(f'SELECT * FROM "{table}" WHERE {where}', tuple(params))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def keys_of(conn: sqlite3.Connection, e: entities.Entity) -> list:
    (col,) = e.key_columns
    return sorted(r[0] for r in conn.execute(f'SELECT "{col}" FROM "{e.root_table}"'))


def aggregate(conn: sqlite3.Connection, e: entities.Entity, key) -> dict | None:
    """aggregate rows, or None"""
    where, params = entities.root_where(e, key)
    roots = _rows(conn, e.root_table, where, params)
    if not roots:
        return None
    data: dict = {e.root_table: roots[0]}
    for child in e.children:
        w, p = entities.child_where(e, child, key)
        rows = _rows(conn, child.table, w, p)
        if child.table in ONE_TO_ONE:
            if rows:
                data[child.table] = rows[0]
        else:
            data.setdefault(child.table, [])
            data[child.table] = sorted(data[child.table] + rows, key=_sortkey)
    return data


def aggregate_at(revision: int, entity: str, key, extra_icons: int = 0) -> dict | None:
    return aggregate(state_db(revision, extra_icons), entities.entity(entity), key)


_SHARED_ORDER = ("element", "icon", "item", "skill_effect", "proc_family", "proc", "passive_skill", "weapon_ability")
_DELETE_ORDER = ("passive_skill", "weapon_ability", "proc", "proc_family", "skill_effect", "item", "icon", "element")
_RETIRE_COLUMNS = {"state", "retired_revision", "last_changed_revision"}


def _last_change(entity: str, key, r: int, n: int, extra: int) -> int:
    """largest differing revision of aggregate"""
    for k in range(n, r, -1):
        if aggregate_at(k, entity, key, extra) != aggregate_at(k - 1, entity, key, extra):
            return k
    return n


def _only_retired(a: dict, b: dict) -> bool:
    if a is None or b is None or a == b:
        return False
    if {k: v for k, v in a.items() if k != "equipment"} != {k: v for k, v in b.items() if k != "equipment"}:
        return False
    ra, rb = a["equipment"], b["equipment"]
    changed = {c for c in ra if ra[c] != rb[c]}
    return changed <= _RETIRE_COLUMNS and ra["state"] == "live" and rb["state"] == "retired"


@functools.cache
def delta_pack_bytes(r: int, n: int, extra_icons: int = 0) -> bytes:
    return make_pack(f"delta-{r}-{n}.zip", "delta", r, n, added_icons(r, n, extra_icons))


def added_icons(r: int, n: int, extra_icons: int = 0) -> list[str]:
    a = {row["sha256"] for row in make_states(extra_icons)[r]["icon"]}
    b = {row["sha256"] for row in make_states(extra_icons)[n]["icon"]}
    return sorted(b - a)


def make_package(from_revision: int, to_revision: int, *, extra_icons: int = 0,
                 dataset_schema_version: int = 1) -> dict:
    """net package between two revisions"""
    r, n = from_revision, to_revision
    a, b = state_db(r, extra_icons), state_db(n, extra_icons)
    ops: list[dict] = []

    def upsert(entity: str, key, rev: int, data: dict) -> dict:
        e = entities.entity(entity)
        return {"op": "UPSERT", "entity": entity, "key": entities.key_to_text(e, key), "rev": rev, "data": data}

    def changed(entity: str) -> list[tuple[object, dict, dict | None]]:
        e = entities.entity(entity)
        out = []
        for key in keys_of(b, e):
            new, old = aggregate(b, e, key), aggregate(a, e, key)
            if new != old:
                out.append((key, new, old))
        return out

    for entity in _SHARED_ORDER:
        for key, new, _old in changed(entity):
            ops.append(upsert(entity, key, _last_change(entity, key, r, n, extra_icons), new))
    retires, upserts = [], []
    for key, new, old in changed("equipment"):
        if _only_retired(old, new):
            retires.append({"op": "RETIRE", "entity": "equipment", "key": key,
                            "rev": new["equipment"]["retired_revision"], "reason": _retire_reason(b, key)})
        else:
            upserts.append(upsert("equipment", key, _last_change("equipment", key, r, n, extra_icons), new))
    ops += upserts + retires
    aliases = []
    for key, new, old in changed("uid_alias"):
        row = new["uid_alias"]
        if old is None:
            aliases.append({"op": "ALIAS", "entity": "equipment", "key": row["old_uid"], "to": row["new_uid"],
                            "rev": row["since_revision"], "confirmed_commit": row["confirmed_commit"]})
    ops += aliases
    for entity in ("uid_retired", "uid_alias"):
        for key, new, _old in changed(entity):
            ops.append(upsert(entity, key, new[entity]["since_revision"], new))
    for entity in _DELETE_ORDER:
        e = entities.entity(entity)
        for key in sorted(set(keys_of(a, e)) - set(keys_of(b, e))):
            ops.append({"op": "DELETE", "entity": entity, "key": entities.key_to_text(e, key),
                        "rev": _last_change(entity, key, r, n, extra_icons)})
    added = added_icons(r, n, extra_icons)
    icons = {"pack": f"icons/packs/delta-{r}-{n}.zip" if added else None,
             "pack_sha256": sha256_hex(delta_pack_bytes(r, n, extra_icons)) if added else None, "added": added}
    return {"from_revision": r, "to_revision": n, "dataset_schema_version": dataset_schema_version,
            "ops": ops, "icons": icons}


def _retire_reason(conn: sqlite3.Connection, uid: str) -> str:
    return conn.execute("SELECT reason FROM uid_retired WHERE uid = ?", (uid,)).fetchone()[0]


def package_bytes(package: Mapping) -> bytes:
    return gz(json.dumps(package, indent=2).encode("utf-8"))


# icon packs
def pack_index(name: str, pack_type: str, from_revision: int | None, to_revision: int,
               shas: Sequence[str]) -> dict:
    return {"name": name, "pack_type": pack_type, "from_revision": from_revision, "to_revision": to_revision,
            "icons": [{"sha256": sha, "kind": "equipment", "bytes": len(ICON_BYTES[sha]),
                       "path": f"equipment/{sha}.png"} for sha in shas]}


def zip_members(members: Sequence[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in members:
            zf.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), data)
    return buf.getvalue()


def make_pack(name: str, pack_type: str, from_revision: int | None, to_revision: int, shas: Sequence[str]) -> bytes:
    index = pack_index(name, pack_type, from_revision, to_revision, shas)
    members = [("index.json", json.dumps(index, indent=2).encode())]
    members += [(f"equipment/{sha}.png", ICON_BYTES[sha]) for sha in shas]
    return zip_members(members)


# pub/ tree
@dataclass
class Pub:
    dir: Path
    key: Keypair
    revision: int
    minimum_revision: int
    extra_icons: int = 0

    @property
    def public_hex(self) -> str:
        return self.key.public_hex

    def path(self, rel: str) -> Path:
        return self.dir / rel

    def read(self, rel: str) -> bytes:
        return self.path(rel).read_bytes()

    def write(self, rel: str, data: bytes) -> None:
        target = self.path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def current(self) -> dict:
        return json.loads(self.read("current.json"))

    def manifest(self, revision: int | None = None) -> dict:
        return json.loads(self.read(f"manifests/{revision or self.revision}.json"))

    def package(self, from_revision: int) -> dict:
        return json.loads(gzip.decompress(self.read(f"updates/{from_revision}-{self.revision}.json.gz")))

    def snapshot_gz(self, revision: int | None = None) -> bytes:
        return self.read(f"snapshots/unison-{revision or self.revision}.sqlite.gz")

    def write_current(self, doc: Mapping, *, sign: bool = True, key: Keypair | None = None) -> bytes:
        doc = sign_current(dict(doc), key or self.key) if sign else dict(doc)
        data = encode_json(doc)
        self.write("current.json", data)
        return data


def build_snapshot(revision: int, dest: Path, *, extra_icons: int = 0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    state_db(revision, extra_icons).execute("VACUUM INTO ?", (str(dest),))
    return dest


def snapshot_gz_bytes(revision: int, tmp: Path, *, extra_icons: int = 0) -> bytes:
    path = build_snapshot(revision, tmp / f"unison-{revision}.sqlite", extra_icons=extra_icons)
    return gz(path.read_bytes())


def mutate_sqlite(src: Path, dst: Path, *statements: str) -> Path:
    dst.write_bytes(src.read_bytes())
    conn = sqlite3.connect(dst, isolation_level=None)
    try:
        for sql in statements:
            conn.executescript(sql)
    finally:
        conn.close()
    return dst


def _stored(rel: str, data: bytes) -> dict:
    return {"path": rel, "sha256": sha256_hex(data), "bytes": len(data)}


def build_pub(root: Path, *, key: Keypair = KEY, revision: int = LATEST, minimum_revision: int = 1,
              snapshot_only: bool = False, extra_icons: int = 0, withdrawn: Sequence[str] = (),
              dataset_schema_version: int = 1, api_schema_version: int = 1,
              published_at: str = PUBLISHED_AT, checked_at: str = CHECKED_AT, expires_at: str = EXPIRES_AT,
              package_hook: Callable[[int, dict], dict] | None = None,
              manifest_hook: Callable[[dict], dict] | None = None,
              current_hook: Callable[[dict], dict] | None = None, validate: bool = True) -> Pub:
    n = revision
    if snapshot_only:
        minimum_revision = n
    pub = Pub(root / "pub", key, n, minimum_revision, extra_icons)
    pub.dir.mkdir(parents=True, exist_ok=True)
    states = make_states(extra_icons)

    for r in range(1, n + 1):
        pub.write(f"snapshots/unison-{r}.sqlite.gz", snapshot_gz_bytes(r, root, extra_icons=extra_icons))
    snapshot = _stored(f"snapshots/unison-{n}.sqlite.gz", pub.snapshot_gz(n))

    updates, deltas = [], []
    if not snapshot_only:
        for r in range(minimum_revision, n):
            package = make_package(r, n, extra_icons=extra_icons, dataset_schema_version=dataset_schema_version)
            if package_hook is not None:
                package = package_hook(r, package)
            rel = f"updates/{r}-{n}.json.gz"
            pub.write(rel, package_bytes(package))
            updates.append({"from_revision": r, **_stored(rel, pub.read(rel))})
            added = added_icons(r, n, extra_icons)
            if added:
                pack_rel = f"icons/packs/delta-{r}-{n}.zip"
                pub.write(pack_rel, delta_pack_bytes(r, n, extra_icons))
                deltas.append({"from_revision": r, **_stored(pack_rel, pub.read(pack_rel)), "icon_count": len(added)})

    base_shas = sorted(row["sha256"] for row in states[n]["icon"])
    base_rel = f"icons/packs/base-{n}.zip"
    pub.write(base_rel, make_pack(f"base-{n}.zip", "base", None, n, base_shas))
    base = {**_stored(base_rel, pub.read(base_rel)), "icon_count": len(base_shas)}
    for sha in sorted({row["sha256"] for r in range(1, n + 1) for row in states[r]["icon"]}):
        pub.write(f"icons/equipment/{sha}.png", ICON_BYTES[sha])
    pub.write(f"lineage/{n}.json.gz", gz(b"{}\n"))

    manifest = {
        "revision": n, "dataset_schema_version": dataset_schema_version, "api_schema_version": api_schema_version,
        "minimum_revision": minimum_revision, "snapshot_only": snapshot_only,
        "snapshot_only_reasons": ["forced"] if snapshot_only else [],
        "freshness": {"source_last_updated": "2026-09-30T00:00:00Z", "dataset_generated_at": PUBLISHED_AT},
        "build": {"build_commit": COMMIT, "transform_run_id": f"t-{n}", "crawl_run_id": "7", "seed_sha256": "ab" * 32,
                  "parser_version": 1, "catalog_version": "cd" * 32, "catalog_commit": COMMIT,
                  "policy_version": "ef" * 32, "policy_commit": COMMIT,
                  "runtime": {"python": "3.12.0", "duckdb": "1.5.6", "dbt_core": "1.12.5", "dbt_duckdb": "1.11.0"}},
        "entity_counts": entity_counts(n, extra_icons),
        "artifacts": {"snapshot": snapshot, "updates": updates,
                      "icon_packs": {"base": base, "deltas": deltas},
                      "lineage": _stored(f"lineage/{n}.json.gz", pub.read(f"lineage/{n}.json.gz"))},
        "icons": {"count": len(base_shas), "withdrawn": list(withdrawn)},
        "gate": {"level": "pass", "warnings": []},
    }
    if manifest_hook is not None:
        manifest = manifest_hook(manifest)
    pub.write(f"manifests/{n}.json", encode_json(manifest))

    current = {"revision": n, "dataset_schema_version": dataset_schema_version,
               "api_schema_version": api_schema_version, "minimum_revision": minimum_revision,
               "manifest": f"manifests/{n}.json", "manifest_sha256": sha256_hex(pub.read(f"manifests/{n}.json")),
               "published_at": published_at, "checked_at": checked_at, "expires_at": expires_at}
    if current_hook is not None:
        current = current_hook(current)
    pub.write_current(current, key=key)
    if validate:
        validate_pub(pub)
    return pub


# contract validation
def contract_available() -> bool:
    return SCHEMAS.is_dir() and any(SCHEMAS.glob("*.schema.json"))


@functools.cache
def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text())


def validate_doc(name: str, doc) -> None:
    if not contract_available():
        return
    from jsonschema import Draft202012Validator

    Draft202012Validator(_schema(name)).validate(doc)


def validate_pub(pub: Pub) -> None:
    if not contract_available():
        return
    validate_doc("current", pub.current())
    validate_doc("manifest", pub.manifest())
    for path in sorted(pub.path("updates").glob("*.json.gz")):
        validate_doc("update_package", json.loads(gzip.decompress(path.read_bytes())))
    for path in sorted(pub.path("icons/packs").glob("*.zip")):
        with zipfile.ZipFile(path) as zf:
            validate_doc("icon_pack", json.loads(zf.read("index.json")))


# api on local server
API_HEADERS = {"X-API-Commit": "dev", "X-Content-Type-Options": "nosniff"}


def _etag(data: bytes) -> str:
    return f'"{sha256_hex(data)[:32]}"'


def api_reply(status: int, body: bytes, content_type: str, **extra: str):
    from local_server import Reply

    return Reply(status, body, {"Content-Type": content_type, "ETag": _etag(body), **API_HEADERS, **extra})


def error_reply(status: int, code: str, message: str = "error"):
    from local_server import Reply

    body = json.dumps({"error": code, "message": message}).encode()
    return Reply(status, body, {"Content-Type": "application/json", "Cache-Control": "no-store", **API_HEADERS})


def requires_snapshot_reply(revision: int, from_revision: int, reason: str, **extra: str):
    body = json.dumps({"requires_snapshot": True, "snapshot_revision": revision, "from_revision": from_revision,
                       "reason": reason}).encode()
    return api_reply(200, body, "application/json", **{"X-UL-To-Revision": str(revision), **extra})


def serve(pub: Pub, server) -> object:
    from local_server import Reply

    def version(request):
        data = pub.read("current.json")
        etag = _etag(data)
        if request.headers.get("If-None-Match") == etag:
            return Reply(304, b"", {"ETag": etag, **API_HEADERS})
        return api_reply(200, data, "application/json", **{"Cache-Control": "public, max-age=300"})

    def static(rel: str, content_type: str):
        def handler(request):
            if not pub.exists(rel):
                return error_reply(404, "not_found")
            return api_reply(200, pub.read(rel), content_type)

        return handler

    def updates(r: int):
        def handler(request):
            current = pub.current()
            n, minimum = current["revision"], current["minimum_revision"]
            if r >= n:
                return error_reply(404, "no_update")
            if r < minimum:
                return requires_snapshot_reply(n, r, "below_minimum")
            rel = f"updates/{r}-{n}.json.gz"
            if not pub.exists(rel):
                return requires_snapshot_reply(n, r, "package_missing")
            return api_reply(200, pub.read(rel), "application/gzip", **{"X-UL-To-Revision": str(n)})

        return handler

    server.route("/v1/version", version)
    for path in sorted(pub.path("manifests").glob("*.json")):
        server.route(f"/v1/manifests/{path.stem}", static(f"manifests/{path.name}", "application/json"))
    for r in range(0, pub.revision + 3):
        server.route(f"/v1/updates/{r}", updates(r))
    for path in sorted(pub.path("snapshots").glob("unison-*.sqlite.gz")):
        n = path.name.split("-")[1].split(".")[0]
        server.route(f"/v1/snapshots/{n}", static(f"snapshots/{path.name}", "application/gzip"))
    for path in sorted(pub.path("icons/packs").glob("*.zip")):
        server.route(f"/v1/icons/packs/{path.name}", static(f"icons/packs/{path.name}", "application/zip"))
    for path in sorted(pub.path("icons").glob("*/*.png")):
        server.route(f"/v1/icons/{path.parent.name}/{path.stem}", static(f"icons/{path.parent.name}/{path.name}",
                                                                          "image/png"))
    for path in sorted(pub.path("lineage").glob("*.json.gz")):
        n = path.name.split(".")[0]
        server.route(f"/v1/lineage/{n}", static(f"lineage/{path.name}", "application/gzip"))
    return server


def make_client(server, **kwargs):
    from ul_house.http.client import RETRY_STATUSES, Client

    options = dict(interval=0.0, max_retries=0, backoff_base=0.0, deadline=30.0, timeout=(2.0, 5.0),
                   retry_statuses=RETRY_STATUSES - {429})
    options.update(kwargs)
    return Client(server.base_url, **options)


# client db
DEFAULT_INVENTORY = (
    (1, 1, Uid.G1, "atk", "def"), (2, 1, Uid.G1, None, None), (3, 1, Uid.W1, "a", "b"),
    (4, 2, Uid.G1, None, None), (5, 2, Uid.M1, "stats1", "stats2"),
)
DEFAULT_SAVED = ((1, Uid.G1), (1, Uid.W1), (2, Uid.G1))


def seed_user_data(conn: sqlite3.Connection, *, inventory: Sequence[tuple] = DEFAULT_INVENTORY,
                   saved: Sequence[tuple] = DEFAULT_SAVED, sets: bool = True) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute("INSERT INTO profile (profile_id, name) VALUES (1, 'alice'), (2, 'bob')")
        for inv_id, profile, uid, s1, s2 in inventory:
            conn.execute("INSERT INTO inventory (inv_id, profile_id, uid, stat_slot1, stat_slot2, acquired_at, note) "
                         "VALUES (?, ?, ?, ?, ?, '2026-09-01T00:00:00Z', ?)", (inv_id, profile, uid, s1, s2, f"n{inv_id}"))
        for profile, uid in saved:
            conn.execute("INSERT INTO saved_ref (profile_id, uid) VALUES (?, ?)", (profile, uid))
        if sets:
            conn.execute("INSERT INTO gear_set (set_id, profile_id, name, cost_cap) VALUES (1, 1, 'Raid', 30)")
            conn.execute("INSERT INTO gear_set_slot (set_id, slot, profile_id, inv_id) VALUES (1, 1, 1, 1), (1, 2, 1, 3)")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def client_db_at(path: Path, revision: int | None, *, user: bool = True, extra_icons: int = 0,
                 app_version: str = "test") -> sqlite3.Connection:
    from ul_house import database

    conn = database.open_database(path)
    if revision is not None:
        conn.execute("BEGIN")
        load_state(conn, make_states(extra_icons)[revision])
        conn.execute("INSERT INTO sync_history (revision, from_revision, kind, applied_at, app_version, package_sha256) "
                     "VALUES (?, NULL, 'snapshot', '2026-10-01T00:00:00Z', ?, ?)", (revision, app_version, "0" * 64))
        conn.execute("COMMIT")
    if user and revision is not None:
        seed_user_data(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("PRAGMA defer_foreign_keys = ON")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def everything(conn: sqlite3.Connection, *, rowid: bool = False) -> dict[str, list[tuple]]:
    """game, ledger, history and user tables"""
    return dump(conn, GAME_TABLES + ("sync_history",) + USER_TABLES, rowid=rowid)


def load_package(package: Mapping):
    """UpdatePackage and its sha256"""
    from ul_house.sync import protocol

    return protocol.UpdatePackage.from_json(json.loads(json.dumps(package))), sha256_hex(package_bytes(package))


def sample_current_doc(**changes) -> dict:
    """a valid, unsigned current.json document"""
    doc = {"revision": 4, "dataset_schema_version": 1, "api_schema_version": 1, "minimum_revision": 1,
           "manifest": "manifests/4.json", "manifest_sha256": "ab" * 32, "published_at": PUBLISHED_AT,
           "checked_at": CHECKED_AT, "expires_at": EXPIRES_AT}
    doc.update(changes)
    return doc


def run_sync(conn, client, pub: Pub, tmp_path: Path, *, cache=None, now=None, keys=None, app_version: str = "test",
             **extra):
    from ul_house.sync import icons, service, verify

    cache = cache or icons.IconCache(tmp_path / "icons")
    keys = keys if keys is not None else verify.load_public_keys([pub.public_hex])
    return service.sync_once(conn, client, cache, keys=keys, app_version=app_version, tmp_dir=tmp_path / "tmp",
                             now=now or (lambda: NOW), **extra)


def make_bundle(directory: Path, revision: int, *, doc=None, file_name: str | None = None, write_file: bool = True,
                extra_icons: int = 0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    name = file_name or f"unison-{revision}.sqlite.gz"
    if write_file:
        (directory / Path(name).name).write_bytes(snapshot_gz_bytes(revision, directory.parent / "bundle-build",
                                                                    extra_icons=extra_icons))
    document = doc if doc is not None else {"revision": revision, "file": name}
    (directory / "bundle.json").write_text(json.dumps(document) if not isinstance(document, str) else document)
    return directory
