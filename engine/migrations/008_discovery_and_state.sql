-- Autonomous onboarding: discovery queue + orchestrator phase state +
-- per-day POST counters (ramp + daily caps enforced by engine/policy.py).
--
-- Only full-line -- comments here; the migration splitter's regex
-- (engine/db.py:_LINE_COMMENT_RE) does NOT strip trailing inline comments,
-- so a stray semicolon inside one silently breaks statement boundaries.

-- Discovery candidates surfaced by the discovery skills.
-- One row per (town_slug, canonical_url, kind); the same URL can legitimately
-- represent both a business (Chamber page) and a community (member org page),
-- so kind is part of the identity.
CREATE TABLE IF NOT EXISTS discovery_candidates (
  candidate_uid          TEXT PRIMARY KEY,
  town_slug              TEXT NOT NULL,
  kind                   TEXT NOT NULL,
  target_category_slug   TEXT,
  canonical_url          TEXT NOT NULL,
  display_name           TEXT,
  status                 TEXT NOT NULL DEFAULT 'new',
  source_signal          TEXT,
  reject_reason          TEXT,
  first_seen_at          TEXT NOT NULL,
  last_seen_at           TEXT NOT NULL,
  verify_json            TEXT,
  research_entity_uid    TEXT,
  UNIQUE (town_slug, canonical_url, kind)
);

CREATE INDEX IF NOT EXISTS idx_discovery_status
  ON discovery_candidates(status, kind);
CREATE INDEX IF NOT EXISTS idx_discovery_new
  ON discovery_candidates(kind, first_seen_at)
  WHERE status = 'new';
CREATE INDEX IF NOT EXISTS idx_discovery_verified
  ON discovery_candidates(kind, first_seen_at)
  WHERE status = 'verified';

-- Single-row orchestrator state. CHECK constraint enforces the singleton.
-- Phase enum: bootstrapping, discovering, verifying, researching,
-- enriching, shipping, steady_state.
-- phase_ticks counts ticks in the current phase and drives the 168-tick
-- force-advance cap; quiet_ticks counts consecutive no-op ticks and drives
-- the transition heuristics per orchestrator design.
CREATE TABLE IF NOT EXISTS agent_state (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  phase          TEXT NOT NULL DEFAULT 'bootstrapping',
  entered_at     TEXT NOT NULL,
  last_tick_at   TEXT,
  tick_count     INTEGER NOT NULL DEFAULT 0,
  phase_ticks    INTEGER NOT NULL DEFAULT 0,
  quiet_ticks    INTEGER NOT NULL DEFAULT 0,
  notes          TEXT
);

-- Per-day per-resource POST counters. Enforces daily caps + onboarding ramp.
-- day is YYYY-MM-DD in AGENT_TZ; a new day gets its own row implicitly.
CREATE TABLE IF NOT EXISTS post_counters (
  day        TEXT NOT NULL,
  resource   TEXT NOT NULL,
  count      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, resource)
);

CREATE INDEX IF NOT EXISTS idx_post_counters_day ON post_counters(day);
