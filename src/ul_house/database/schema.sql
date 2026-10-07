-- dataset_schema_version: 1

-- @section game
CREATE TABLE IF NOT EXISTS element (
    element_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS element_relation (
    element_id TEXT NOT NULL REFERENCES element(element_id),
    related_id TEXT NOT NULL REFERENCES element(element_id),
    kind       TEXT NOT NULL CHECK (kind IN ('effective', 'weakness')),
    PRIMARY KEY (element_id, related_id, kind)
);

-- non equips
CREATE TABLE IF NOT EXISTS item (
    uid  TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS icon (
    sha256 TEXT    PRIMARY KEY,
    kind   TEXT    NOT NULL CHECK (kind IN ('equipment', 'item', 'ability')),
    bytes  INTEGER NOT NULL
);

-- combat mechs
CREATE TABLE IF NOT EXISTS skill_effect (
    effect_id   INTEGER PRIMARY KEY,
    target      TEXT,
    description TEXT NOT NULL,
    UNIQUE (target, description)
);

CREATE TABLE IF NOT EXISTS proc_family (
    family_id INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS proc (
    proc_id          INTEGER PRIMARY KEY,
    family_id        INTEGER NOT NULL REFERENCES proc_family(family_id),
    element_id       TEXT REFERENCES element(element_id),
    size             TEXT,
    raw_name         TEXT NOT NULL,
    activation_rate  TEXT,
    element_position TEXT CHECK (element_position IN ('prefix', 'suffix', 'infix'))
);

CREATE INDEX IF NOT EXISTS ix_proc_family_element_size ON proc(family_id, element_id, size);

CREATE TABLE IF NOT EXISTS proc_condition (
    proc_id   INTEGER NOT NULL REFERENCES proc(proc_id) ON DELETE CASCADE,
    ordinal   INTEGER NOT NULL,
    condition TEXT    NOT NULL,
    PRIMARY KEY (proc_id, ordinal)
);

CREATE TABLE IF NOT EXISTS proc_scaling (
    proc_id       INTEGER NOT NULL REFERENCES proc(proc_id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    scale_kind    TEXT    NOT NULL,                         -- ability_power | activation_rate
    element_id    TEXT REFERENCES element(element_id),
    gear_kind     TEXT,
    cap           TEXT,                                     -- 5XL+, etc
    pieces_needed INTEGER,
    description   TEXT    NOT NULL,
    PRIMARY KEY (proc_id, ordinal)
);

CREATE INDEX IF NOT EXISTS ix_proc_scaling_element ON proc_scaling(element_id);

CREATE TABLE IF NOT EXISTS weapon_ability (
    uid  TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS passive_skill (
    passive_id  INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    effect_hash TEXT NOT NULL,
    UNIQUE (name, effect_hash)
);

-- class table inheritance
CREATE TABLE IF NOT EXISTS equipment (
    uid                   TEXT    PRIMARY KEY,
    name                  TEXT    NOT NULL,
    rarity                TEXT    NOT NULL,
    gear_type             TEXT    NOT NULL,
    cost                  INTEGER NOT NULL,
    element_id            TEXT    NOT NULL REFERENCES element(element_id),
    max_level             INTEGER NOT NULL,
    icon_sha              TEXT,
    entry_kind            TEXT    NOT NULL CHECK (entry_kind IN ('catalog', 'reference')),
    keep_reason           TEXT    NOT NULL,
    evo_depth             INTEGER NOT NULL DEFAULT 0,
    state                 TEXT    NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'retired')),
    retired_revision      INTEGER,
    first_seen            TEXT    NOT NULL,
    last_seen             TEXT    NOT NULL,
    last_changed_revision INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_equipment_visible ON equipment(entry_kind, state);
CREATE INDEX IF NOT EXISTS ix_equipment_type_cost ON equipment(gear_type, cost);
CREATE INDEX IF NOT EXISTS ix_equipment_name ON equipment(name);

CREATE TABLE IF NOT EXISTS weapon (
    uid            TEXT    PRIMARY KEY REFERENCES equipment(uid) ON DELETE CASCADE,
    infusion_count INTEGER NOT NULL DEFAULT 0,
    proc_id        INTEGER REFERENCES proc(proc_id),
    ability_uid    TEXT    REFERENCES weapon_ability(uid)
);

CREATE INDEX IF NOT EXISTS ix_weapon_proc ON weapon(proc_id);
CREATE INDEX IF NOT EXISTS ix_weapon_ability ON weapon(ability_uid);

CREATE TABLE IF NOT EXISTS defensive_gear (
    uid            TEXT    PRIMARY KEY REFERENCES equipment(uid) ON DELETE CASCADE,
    infusion_count INTEGER NOT NULL DEFAULT 0,
    proc_id        INTEGER REFERENCES proc(proc_id)
);

CREATE INDEX IF NOT EXISTS ix_defensive_gear_proc ON defensive_gear(proc_id);

CREATE TABLE IF NOT EXISTS monster (
    uid          TEXT    PRIMARY KEY REFERENCES equipment(uid) ON DELETE CASCADE,
    passive_id   INTEGER REFERENCES passive_skill(passive_id),
    restrictions TEXT
);

CREATE INDEX IF NOT EXISTS ix_monster_passive ON monster(passive_id);

CREATE TABLE IF NOT EXISTS monster_skill (
    skill_id INTEGER PRIMARY KEY,
    uid      TEXT    NOT NULL REFERENCES monster(uid) ON DELETE CASCADE,
    ordinal  INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    UNIQUE (uid, ordinal)
);

CREATE TABLE IF NOT EXISTS potential_level (
    uid       TEXT    NOT NULL REFERENCES monster(uid) ON DELETE CASCADE,
    level     INTEGER NOT NULL,
    effect_id INTEGER NOT NULL REFERENCES skill_effect(effect_id),
    PRIMARY KEY (uid, level)
);

CREATE TABLE IF NOT EXISTS effect_link (
    owner_kind TEXT    NOT NULL CHECK (owner_kind IN
                 ('proc', 'weapon_ability', 'monster_skill', 'passive_skill', 'potential_level')),
    owner_id   TEXT    NOT NULL,
    ordinal    INTEGER NOT NULL,
    effect_id  INTEGER NOT NULL REFERENCES skill_effect(effect_id),
    PRIMARY KEY (owner_kind, owner_id, ordinal)
);

CREATE INDEX IF NOT EXISTS ix_effect_link_owner ON effect_link(owner_kind, owner_id);
CREATE INDEX IF NOT EXISTS ix_effect_link_effect ON effect_link(effect_id);

CREATE TABLE IF NOT EXISTS stat (
    uid   TEXT    NOT NULL REFERENCES equipment(uid) ON DELETE CASCADE,
    label TEXT    NOT NULL,                             -- atk matk def mdef stats1 stats2
    tier  TEXT    NOT NULL,                             -- initial max max1 max2
    value INTEGER NOT NULL,
    PRIMARY KEY (uid, label, tier)
);

-- evos
CREATE TABLE IF NOT EXISTS evolution_edge (
    kind      TEXT NOT NULL CHECK (kind IN ('reforge', 'awakening', 'enlightening')),
    from_uid  TEXT NOT NULL,
    to_uid    TEXT NOT NULL,
    evidence  TEXT NOT NULL CHECK (evidence IN ('before', 'after', 'both')),
    from_name TEXT NOT NULL,
    to_name   TEXT NOT NULL,
    PRIMARY KEY (kind, from_uid, to_uid)
);

CREATE INDEX IF NOT EXISTS ix_evolution_edge_from ON evolution_edge(from_uid);
CREATE INDEX IF NOT EXISTS ix_evolution_edge_to ON evolution_edge(to_uid);

CREATE TABLE IF NOT EXISTS evolution_chain (
    kind     TEXT    NOT NULL CHECK (kind IN ('reforge', 'awakening', 'enlightening')),
    chain_id TEXT    NOT NULL,
    uid      TEXT    NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (kind, uid)
);

CREATE INDEX IF NOT EXISTS ix_evolution_chain_chain ON evolution_chain(kind, chain_id, position);

CREATE TABLE IF NOT EXISTS evolution_material (
    kind          TEXT    NOT NULL CHECK (kind IN ('reforge', 'awakening', 'enlightening')),
    from_uid      TEXT    NOT NULL,
    to_uid        TEXT    NOT NULL,
    ordinal       INTEGER NOT NULL,
    material_kind TEXT    NOT NULL CHECK (material_kind IN ('gear', 'item')),
    ref_uid       TEXT,
    ref_name      TEXT    NOT NULL,
    quantity      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (kind, from_uid, to_uid, ordinal)
);

CREATE INDEX IF NOT EXISTS ix_evolution_material_from ON evolution_material(from_uid);
CREATE INDEX IF NOT EXISTS ix_evolution_material_to ON evolution_material(to_uid);
CREATE INDEX IF NOT EXISTS ix_evolution_material_ref ON evolution_material(ref_uid);

-- @section ledger

CREATE TABLE IF NOT EXISTS uid_retired (
    uid            TEXT    PRIMARY KEY,
    since_revision INTEGER NOT NULL,
    reason         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS uid_alias (
    old_uid          TEXT    PRIMARY KEY,
    new_uid          TEXT    NOT NULL,
    since_revision   INTEGER NOT NULL,
    confirmed_commit TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_uid_alias_new ON uid_alias(new_uid);

-- @section sync

CREATE TABLE IF NOT EXISTS sync_history (
    revision       INTEGER PRIMARY KEY,
    from_revision  INTEGER,
    kind           TEXT    NOT NULL CHECK (kind IN ('update', 'snapshot')),
    applied_at     TEXT    NOT NULL,
    app_version    TEXT    NOT NULL,
    package_sha256 TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- @section user

CREATE TABLE IF NOT EXISTS profile (
    profile_id INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS inventory (
    inv_id      INTEGER PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profile(profile_id) ON DELETE CASCADE,
    uid         TEXT    NOT NULL REFERENCES equipment(uid) DEFERRABLE INITIALLY DEFERRED,
    stat_slot1  TEXT,
    stat_slot2  TEXT,
    acquired_at TEXT,
    note        TEXT,
    UNIQUE (profile_id, inv_id)
);

CREATE INDEX IF NOT EXISTS ix_inventory_uid ON inventory(uid);
CREATE INDEX IF NOT EXISTS ix_inventory_profile_uid ON inventory(profile_id, uid);

CREATE TABLE IF NOT EXISTS saved_ref (
    profile_id INTEGER NOT NULL REFERENCES profile(profile_id) ON DELETE CASCADE,
    uid        TEXT    NOT NULL REFERENCES equipment(uid) DEFERRABLE INITIALLY DEFERRED,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (profile_id, uid)
);

CREATE INDEX IF NOT EXISTS ix_saved_ref_uid ON saved_ref(uid);

CREATE TABLE IF NOT EXISTS gear_set (
    set_id     INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profile(profile_id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    cost_cap   INTEGER,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (profile_id, set_id)
);

CREATE TABLE IF NOT EXISTS gear_set_slot (
    set_id     INTEGER NOT NULL,
    slot       INTEGER NOT NULL,
    profile_id INTEGER NOT NULL,
    inv_id     INTEGER NOT NULL,
    PRIMARY KEY (set_id, slot),
    UNIQUE (set_id, inv_id),
    FOREIGN KEY (profile_id, set_id) REFERENCES gear_set(profile_id, set_id) ON DELETE CASCADE,
    FOREIGN KEY (profile_id, inv_id) REFERENCES inventory(profile_id, inv_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_gear_set_slot_inventory ON gear_set_slot(profile_id, inv_id);

CREATE VIEW IF NOT EXISTS gear_set_status AS
SELECT gs.set_id,
       gs.profile_id,
       gs.name,
       gs.cost_cap,
       COUNT(gss.slot)                                   AS slot_count,
       COALESCE(SUM(e.cost), 0)                          AS total_cost,
       COALESCE(SUM(e.state = 'retired'), 0)             AS retired_count,
       (gs.cost_cap IS NOT NULL
        AND COALESCE(SUM(e.cost), 0) > gs.cost_cap)      AS over_cap
FROM gear_set AS gs
LEFT JOIN gear_set_slot AS gss ON gss.set_id = gs.set_id
LEFT JOIN inventory     AS inv ON inv.profile_id = gss.profile_id AND inv.inv_id = gss.inv_id
LEFT JOIN equipment     AS e   ON e.uid = inv.uid
GROUP BY gs.set_id;

CREATE VIEW IF NOT EXISTS stat_assignable AS
SELECT DISTINCT uid,
       label,
       (instr(lower(label), 'stat') > 0) AS assignable
FROM stat;
