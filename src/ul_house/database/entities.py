from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ul_house import ids

DATASET_SCHEMA_VERSION: int = 1
SCHEMA_PATH: Path = Path(__file__).with_name("schema.sql")
SECTIONS: tuple[str, ...] = ("game", "ledger", "sync", "user")

_SECTION_RE = re.compile(r"^-- @section (game|ledger|sync|user)$", re.M)
_CREATE_TABLE_RE = re.compile(r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I | re.M)


@dataclass(frozen=True, slots=True)
class Via:
    """intermediate table with row aggregation"""
    table: str
    id_columns: tuple[str, ...]     # monster_skill(id), potential_level(id)
    owner_columns: tuple[str, ...]  # columns of table compared to entity key
    key_type: str                   # text | int (val bound to owner_columns)


@dataclass(frozen=True, slots=True)
class Child:
    """rows of specified table owned by an entity"""
    table: str
    owner_columns: tuple[str, ...]
    key_type: str                   # text | int
    owner_kind: str | None = None   # for effect_link children only
    via: Via | None = None          # effect_link of monster_skill / potential_level owners

    @property
    def predicate(self) -> str:
        return _render(self, ":key", ":key_text")


@dataclass(frozen=True, slots=True)
class Entity:
    entity_type: str                # dataset_changes.entity_type
    root_table: str
    key_columns: tuple[str, ...]    # the root table's PK (one col per entity)
    key_type: str                   # text | int
    children: tuple[Child, ...]     # DELETE order: INSERT root first then children in reverse
    deletable: bool
    retirable: bool
    aliasable: bool


def _id_text(column: str) -> str:
    return f"CAST({column} AS TEXT)"


def _match(columns: tuple[str, ...], key_type: str, native: str, text: str) -> str:
    param = native if key_type == "int" else text
    return " OR ".join(f"{column} = {param}" for column in columns)


def _render(child: Child, native: str, text: str) -> str:
    clauses = []
    if child.owner_kind is not None:
        clauses.append(f"owner_kind = '{child.owner_kind}'")
    if child.via is None:
        match = _match(child.owner_columns, child.key_type, native, text)
        clauses.append(f"({match})" if len(child.owner_columns) > 1 else match)
    else:
        via = child.via
        id_expr = " || ':' || ".join(_id_text(column) for column in via.id_columns)
        inner = _match(via.owner_columns, via.key_type, native, text)
        (column,) = child.owner_columns
        clauses.append(f"{column} IN (SELECT {id_expr} FROM {via.table} WHERE {inner})")
    return " AND ".join(clauses)


def _child(table: str, *owner_columns: str, key_type: str = "text") -> Child:
    return Child(table, owner_columns, key_type)


def _links(owner_kind: str, via: Via | None = None) -> Child:
    """effect_link rows of one owner_kind"""
    return Child("effect_link", ("owner_id",), "text", owner_kind=owner_kind, via=via)


def _shared(entity_type: str, root_table: str, key_column: str, key_type: str,
            children: tuple[Child, ...] = ()) -> Entity:
    return Entity(entity_type, root_table, (key_column,), key_type, children,
                  deletable=True, retirable=False, aliasable=False)


def _ledger(entity_type: str, key_column: str) -> Entity:
    return Entity(entity_type, entity_type, (key_column,), "text", (),
                  deletable=False, retirable=False, aliasable=False)


ENTITIES: tuple[Entity, ...] = (
    Entity(
        entity_type="equipment",
        root_table="equipment",
        key_columns=("uid",),
        key_type="text",
        children=(
            _links("monster_skill", Via("monster_skill", ("skill_id",), ("uid",), "text")),
            _links("potential_level", Via("potential_level", ("uid", "level"), ("uid",), "text")),
            _child("monster_skill", "uid"),
            _child("potential_level", "uid"),
            _child("stat", "uid"),
            _child("evolution_edge", "from_uid", "to_uid"),
            _child("evolution_material", "from_uid", "to_uid"),
            _child("evolution_chain", "uid"),
            _child("weapon", "uid"),
            _child("defensive_gear", "uid"),
            _child("monster", "uid"),
        ),
        deletable=False,
        retirable=True,
        aliasable=True,
    ),
    _shared("proc", "proc", "proc_id", "int", (
        _links("proc"),
        _child("proc_condition", "proc_id", key_type="int"),
        _child("proc_scaling", "proc_id", key_type="int"),
    )),
    _shared("proc_family", "proc_family", "family_id", "int"),
    _shared("skill_effect", "skill_effect", "effect_id", "int"),
    _shared("passive_skill", "passive_skill", "passive_id", "int", (_links("passive_skill"),)),
    _shared("weapon_ability", "weapon_ability", "uid", "text", (_links("weapon_ability"),)),
    _shared("item", "item", "uid", "text"),
    _shared("icon", "icon", "sha256", "text"),
    _shared("element", "element", "element_id", "text", (_child("element_relation", "element_id"),)),
    _ledger("uid_retired", "uid"),
    _ledger("uid_alias", "old_uid"),
)

_BY_TYPE: dict[str, Entity] = {e.entity_type: e for e in ENTITIES}


def entity(entity_type: str) -> Entity:
    return _BY_TYPE[entity_type]


def owner(table: str, owner_kind: str | None = None) -> Entity:
    for e in ENTITIES:
        if e.root_table == table:
            return e
    for e in ENTITIES:
        for child in e.children:
            if child.table != table:
                continue
            if child.owner_kind is None or child.owner_kind == owner_kind:
                return e
    if table == "effect_link" and owner_kind is None:
        raise KeyError("effect_link is owned per owner_kind; pass owner_kind")
    raise KeyError(table if owner_kind is None else f"{table}[{owner_kind}]")


def key_to_text(e: Entity, key: int | str) -> str:
    if e.key_type == "int":
        return ids.id_to_text(key)
    if not isinstance(key, str):
        raise TypeError(f"{e.entity_type} key must be str, not {type(key).__name__}")
    return key


def key_from_text(e: Entity, text: str) -> int | str:
    if e.key_type == "int":
        return ids.id_from_text(text)
    if not isinstance(text, str):
        raise TypeError(f"{e.entity_type} key text must be str, not {type(text).__name__}")
    return text


def _bind(e: Entity, key_type: str, key: int | str) -> int | str:
    return key_to_text(e, key) if key_type == "text" else key


def child_where(e: Entity, child: Child, key: int | str) -> tuple[str, tuple]:
    """selecting rows of child.table that the aggregate key of entity owns"""
    sql = _render(child, "?", "?")
    if child.via is None:
        params = tuple(_bind(e, child.key_type, key) for _ in child.owner_columns)
    else:
        params = tuple(_bind(e, child.via.key_type, key) for _ in child.via.owner_columns)
    return sql, params


def root_where(e: Entity, key: int | str) -> tuple[str, tuple]:
    """selecting the root row of key"""
    return " AND ".join(f"{column} = ?" for column in e.key_columns), (key,)


def schema_sections(text: str | None = None) -> dict[str, str]:
    """schema.sql section order via @section <name> comment markers"""
    if text is None:
        text = SCHEMA_PATH.read_text(encoding="utf-8")
    parts = _SECTION_RE.split(text)
    names = tuple(parts[1::2])
    if names != SECTIONS:
        raise ValueError(f"schema sections {names} != {SECTIONS}")
    return dict(zip(names, parts[2::2]))


def section_tables(section: str) -> tuple[str, ...]:
    """create table names via schema_path (file ordered)"""
    return tuple(_CREATE_TABLE_RE.findall(schema_sections()[section]))


GAME_TABLES: tuple[str, ...] = section_tables("game")
LEDGER_TABLES: tuple[str, ...] = section_tables("ledger")
SYNC_TABLES: tuple[str, ...] = section_tables("sync")
USER_TABLES: tuple[str, ...] = section_tables("user")
