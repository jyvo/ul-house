-- dev artifact seed.sqlite (re-parsing whole catalog reads this file and makes no requests)

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- one row per fetched url: list, new-release, and detail pages
CREATE TABLE IF NOT EXISTS page (
    url           TEXT    PRIMARY KEY,                -- path relative to BASE_URL
    kind          TEXT    NOT NULL CHECK (kind IN ('list', 'new_release', 'detail')),
    item_id       TEXT,                               -- detail pages only
    html          BLOB,                               -- zlib level 6 (NULL until first 200)
    html_sha256   TEXT,                               -- of the decompressed body > change detection
    etag          TEXT,
    last_modified TEXT,
    status        INTEGER NOT NULL,                   -- last HTTP status seen (200, 304, 404 ...)
    content_type  TEXT,
    fetched_at    TEXT    NOT NULL,                   -- last request, whatever its outcome
    changed_at    TEXT,                               -- last time html_sha256 changed
    CHECK ((kind = 'detail') = (item_id IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_page_item ON page(item_id) WHERE item_id IS NOT NULL;

-- list row per id, captured at discovery so select() needs no detail fetch
CREATE TABLE IF NOT EXISTS listing (
    item_id     TEXT    PRIMARY KEY,
    source_url  TEXT    NOT NULL REFERENCES page(url),
    name        TEXT    NOT NULL,
    grp         TEXT    NOT NULL CHECK (grp IN ('weapon', 'armor', 'monster')),
    rarity      TEXT    NOT NULL,
    gear_type   TEXT,                                 -- raw attribute type (monster rows carry none)
    element     TEXT    NOT NULL,                     -- raw attribute value
    cost        INTEGER NOT NULL,
    seen_at     TEXT    NOT NULL
);

-- resume checkpoint > one row per id the crawl has decided about
CREATE TABLE IF NOT EXISTS frontier (
    item_id       TEXT    PRIMARY KEY,
    source        TEXT    NOT NULL CHECK (source IN ('list', 'new_release', 'evolution', 'manual')),
    entry_kind    TEXT    NOT NULL CHECK (entry_kind IN ('catalog', 'reference')),
    keep_reason   TEXT    NOT NULL,                   -- cost_band | name_token | evolution:<kind>
    depth         INTEGER NOT NULL DEFAULT 0,         -- evolution hops from a kept item
    parent_id     TEXT,                               -- the item whose link reached this one
    state         TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'in_flight', 'done', 'failed', 'gone')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    discovered_at TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    CHECK ((depth = 0) = (parent_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_frontier_state ON frontier(state);

-- evolution links read off detail pages before reconciliation
CREATE TABLE IF NOT EXISTS evo_link (
    source_id   TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('reforge', 'awakening', 'enlightening')),
    side        TEXT NOT NULL CHECK (side IN ('before', 'after')),
    target_id   TEXT NOT NULL,
    target_name TEXT NOT NULL,                    -- p.evo_name, for name exclusions
    PRIMARY KEY (source_id, kind, side)
);

CREATE INDEX IF NOT EXISTS idx_evo_link_target ON evo_link(target_id);

-- envelope stamp, crawl timestamps, contract version, revalidation
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
