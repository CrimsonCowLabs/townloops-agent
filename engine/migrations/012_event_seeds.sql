-- 012_event_seeds.sql
-- Per-town list of seed sources the agent scrapes for events.
-- LLM-discovered on first-run, operator-curated via the dashboard.

CREATE TABLE IF NOT EXISTS event_seeds (
  seed_id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  name                          TEXT NOT NULL,
  url                           TEXT NOT NULL UNIQUE,
  kind                          TEXT NOT NULL,          -- 'chamber' | 'government' | 'tourism' | 'library' | 'parks' | 'newspaper' | 'venue' | 'other'
  status                        TEXT NOT NULL DEFAULT 'proposed',  -- 'proposed' | 'approved' | 'rejected'
  discovered_by                 TEXT NOT NULL DEFAULT 'llm',       -- 'llm' | 'operator'
  trust_auto_approve            INTEGER NOT NULL DEFAULT 0,        -- 0/1 — approve events from this source without operator gate (still runs confidence filters)
  trust_up_to_events_per_day    INTEGER NOT NULL DEFAULT 20,
  last_scraped_at               TEXT,
  last_event_count              INTEGER,
  consecutive_empty_scrapes     INTEGER NOT NULL DEFAULT 0,
  notes                         TEXT,
  first_seen_at                 TEXT NOT NULL,
  last_seen_at                  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_seeds_status ON event_seeds(status);
CREATE INDEX IF NOT EXISTS idx_seeds_last_scraped ON event_seeds(last_scraped_at);
