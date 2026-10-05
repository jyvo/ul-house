-- dev artifact seed.sqlite (re-parsing whole catalog reads this file and makes no requests)

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- row per crawl
CREATE TABLE IF NOT EXISTS crawl_run (
    run_id             INTEGER PRIMARY KEY,                 -- # run in the current seed
    trigger            TEXT    NOT NULL CHECK (trigger IN ('schedule', 'dispatch', 'manual')),
    started_at         TEXT    NOT NULL,                    -- ISO-8601 UTC, Z suffix from seed.store.utc_now()
    ended_at           TEXT,                                -- NULL while running and for abandoned runs
    code_commit        TEXT    NOT NULL,                    -- unknown if undeterminable
    catalog_version    TEXT    NOT NULL,                    -- scope.fingerprint()
    catalog_commit     TEXT    NOT NULL,
    pages_discovered   INTEGER NOT NULL DEFAULT 0,
    pages_fetched      INTEGER NOT NULL DEFAULT 0,
    pages_changed      INTEGER NOT NULL DEFAULT 0,
    pages_failed       INTEGER NOT NULL DEFAULT 0,
    icons_fetched      INTEGER NOT NULL DEFAULT 0,
    icons_changed      INTEGER NOT NULL DEFAULT 0,
    status             TEXT    NOT NULL CHECK (status IN ('running', 'complete', 'partial', 'failed', 'abandoned')),
    pages_not_modified INTEGER NOT NULL DEFAULT 0,          -- detail 304s
    icons_failed       INTEGER NOT NULL DEFAULT 0,
    shard_index        INTEGER,                             -- which 1/cycle_days shard this run revalidates
    shard_items        INTEGER NOT NULL DEFAULT 0,          -- detail revalidations
    error              TEXT,
    code_dirty         INTEGER NOT NULL DEFAULT 0 CHECK (code_dirty IN (0, 1)),                     -- uncommitted changes in worktree
    new_release_skipped INTEGER NOT NULL DEFAULT 0 CHECK (new_release_skipped IN (0, 1))            -- if page unreadable
);

-- one row per fetched url: list, new-release, and detail pages
CREATE TABLE IF NOT EXISTS page (
    url           TEXT    PRIMARY KEY,                      -- path relative to BASE_URL
    kind          TEXT    NOT NULL CHECK (kind IN ('list', 'new_release', 'detail')),
    item_id       TEXT,                                     -- detail pages only
    html          BLOB,                                     -- zlib level 6 (NULL until first 200)
    html_sha256   TEXT,                                     -- of the decompressed body > change detection
    etag          TEXT,
    last_modified TEXT,
    status        INTEGER NOT NULL,                         -- last HTTP status seen (200, 304, 404 ...)
    content_type  TEXT,
    fetched_at    TEXT    NOT NULL,                         -- last request, whatever its outcome
    changed_at    TEXT,                                     -- last time html_sha256 changed
    run_id         INTEGER REFERENCES crawl_run(run_id),    -- last run that fetched it (any status)
    changed_run_id INTEGER REFERENCES crawl_run(run_id),    -- last run that changed html_sha256
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
    gear_type   TEXT,                                       -- raw attribute type (monster rows carry none)
    element     TEXT    NOT NULL,                           -- raw attribute value
    cost        INTEGER NOT NULL,
    seen_at     TEXT    NOT NULL
);

-- resume checkpoint > one row per id the crawl has decided about
CREATE TABLE IF NOT EXISTS frontier (
    item_id       TEXT    PRIMARY KEY,
    source        TEXT    NOT NULL CHECK (source IN ('list', 'new_release', 'evolution', 'manual')),
    entry_kind    TEXT    NOT NULL CHECK (entry_kind IN ('catalog', 'reference')),
    keep_reason   TEXT    NOT NULL,                         -- cost_band | name_token | evolution:<kind>
    depth         INTEGER NOT NULL DEFAULT 0,               -- evolution hops from a kept item
    parent_id     TEXT,                                     -- the item whose link reached this one
    state         TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'in_flight', 'done', 'failed', 'gone')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    discovered_at TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    decided_run_id INTEGER REFERENCES crawl_run(run_id),    -- last run that reached
    CHECK ((depth = 0) = (parent_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_frontier_state ON frontier(state);
CREATE INDEX IF NOT EXISTS idx_frontier_run   ON frontier(decided_run_id, state);

-- evolution links read off detail pages before reconciliation
CREATE TABLE IF NOT EXISTS evo_link (
    source_id   TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('reforge', 'awakening', 'enlightening')),
    side        TEXT NOT NULL CHECK (side IN ('before', 'after')),
    target_id   TEXT NOT NULL,
    target_name TEXT NOT NULL,                              -- p.evo_name, for name exclusions
    PRIMARY KEY (source_id, kind, side)
);

CREATE INDEX IF NOT EXISTS idx_evo_link_target ON evo_link(target_id);

-- icon bytes + validators
CREATE TABLE IF NOT EXISTS asset (
    url            TEXT    PRIMARY KEY,
    item_id        TEXT    NOT NULL,
    kind           TEXT    NOT NULL CHECK (kind IN ('equipment', 'item', 'ability')),
    body           BLOB,
    sha256         TEXT,
    etag           TEXT,
    last_modified  TEXT,
    status         INTEGER,                                 -- last HTTP status | NULL
    content_type   TEXT,
    fetched_at     TEXT,                                    -- last request | NULL
    changed_at     TEXT,                                    -- last time sha256 changed
    run_id         INTEGER REFERENCES crawl_run(run_id),    -- last run that fetched it
    changed_run_id INTEGER REFERENCES crawl_run(run_id),    -- last run that changed sha256
    CHECK ((body IS NULL) = (sha256 IS NULL)),
    CHECK ((status IS NULL) = (fetched_at IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_asset_item ON asset(item_id);

-- new release ids with no list page showing
CREATE TABLE IF NOT EXISTS unlisted_release (
    item_id      TEXT    PRIMARY KEY,
    name         TEXT    NOT NULL,
    first_run_id INTEGER NOT NULL REFERENCES crawl_run(run_id),
    last_run_id  INTEGER NOT NULL REFERENCES crawl_run(run_id),
    runs         INTEGER NOT NULL CHECK (runs >= 1)       -- consecutive runs unlisted
);

-- envelope stamp, crawl timestamps, contract version, revalidation
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO meta (key, value) VALUES ('seed.schema_version', '1');
