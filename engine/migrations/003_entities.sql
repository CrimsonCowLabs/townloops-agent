-- Phase B / Phase C: Townloops entity expansion.
-- Five tables (one per entity kind) mirroring the events table's
-- audit/ship-state columns plus per-kind fields matching the Townloops
-- payload contracts exactly.
--
-- entity_uid = sha1(kind|name_norm). Same name across sources collapses
-- to one row; per-kind endpoints in Townloops keep slug pools separate.

CREATE TABLE IF NOT EXISTS venues (
  entity_uid       TEXT PRIMARY KEY,
  source           TEXT NOT NULL,
  source_kind      TEXT NOT NULL,   -- 'mined' | 'manual'
  name             TEXT NOT NULL,
  description      TEXT,
  address          TEXT,
  website          TEXT,
  phone            TEXT,
  accessibility_notes TEXT,
  image_path       TEXT,
  image_alt        TEXT,
  category_slug    TEXT,
  source_url       TEXT,
  extra_json       TEXT,
  first_seen_at    TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  content_hash     TEXT NOT NULL,
  last_modified_at TEXT NOT NULL,
  pushed_at        TEXT,
  pushed_hash      TEXT
);
CREATE INDEX IF NOT EXISTS idx_venues_unpushed ON venues(pushed_at) WHERE pushed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_venues_source ON venues(source);

CREATE TABLE IF NOT EXISTS businesses (
  entity_uid       TEXT PRIMARY KEY,
  source           TEXT NOT NULL,
  source_kind      TEXT NOT NULL,
  name             TEXT NOT NULL,
  description      TEXT,
  address          TEXT,
  website          TEXT,
  phone            TEXT,
  category_slug    TEXT,
  source_url       TEXT,
  extra_json       TEXT,
  first_seen_at    TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  content_hash     TEXT NOT NULL,
  last_modified_at TEXT NOT NULL,
  pushed_at        TEXT,
  pushed_hash      TEXT
);
CREATE INDEX IF NOT EXISTS idx_businesses_unpushed ON businesses(pushed_at) WHERE pushed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_businesses_source ON businesses(source);

CREATE TABLE IF NOT EXISTS communities (
  entity_uid       TEXT PRIMARY KEY,
  source           TEXT NOT NULL,
  source_kind      TEXT NOT NULL,
  name             TEXT NOT NULL,
  kind             TEXT,            -- Townloops community.kind (default 'group')
  description      TEXT,
  website          TEXT,
  meeting_info     TEXT,
  category_slug    TEXT,
  source_url       TEXT,
  extra_json       TEXT,
  first_seen_at    TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  content_hash     TEXT NOT NULL,
  last_modified_at TEXT NOT NULL,
  pushed_at        TEXT,
  pushed_hash      TEXT
);
CREATE INDEX IF NOT EXISTS idx_communities_unpushed ON communities(pushed_at) WHERE pushed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_communities_source ON communities(source);

CREATE TABLE IF NOT EXISTS performers (
  entity_uid       TEXT PRIMARY KEY,
  source           TEXT NOT NULL,
  source_kind      TEXT NOT NULL,
  name             TEXT NOT NULL,
  kind             TEXT,            -- Townloops performer.kind (default 'band')
  description      TEXT,
  website          TEXT,
  instagram        TEXT,
  spotify_url      TEXT,
  image_path       TEXT,
  image_alt        TEXT,
  source_url       TEXT,
  extra_json       TEXT,
  first_seen_at    TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  content_hash     TEXT NOT NULL,
  last_modified_at TEXT NOT NULL,
  pushed_at        TEXT,
  pushed_hash      TEXT
);
CREATE INDEX IF NOT EXISTS idx_performers_unpushed ON performers(pushed_at) WHERE pushed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_performers_source ON performers(source);

CREATE TABLE IF NOT EXISTS creators (
  entity_uid       TEXT PRIMARY KEY,
  source           TEXT NOT NULL,
  source_kind      TEXT NOT NULL,
  name             TEXT NOT NULL,
  kind             TEXT,            -- Townloops creator.kind (default 'other')
  bio              TEXT,
  website          TEXT,
  instagram        TEXT,
  youtube_url      TEXT,
  image_path       TEXT,
  image_alt        TEXT,
  source_url       TEXT,
  extra_json       TEXT,
  first_seen_at    TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  content_hash     TEXT NOT NULL,
  last_modified_at TEXT NOT NULL,
  pushed_at        TEXT,
  pushed_hash      TEXT
);
CREATE INDEX IF NOT EXISTS idx_creators_unpushed ON creators(pushed_at) WHERE pushed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_creators_source ON creators(source);

-- Per-ingest entity counts roll up here so we have one audit row per run.
ALTER TABLE source_runs ADD COLUMN entity_counts_json TEXT;
