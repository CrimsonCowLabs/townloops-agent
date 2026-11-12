-- 032 — discovery_checks: what source hunting and organisation harvest
-- have already looked at, so a daily run moves on instead of repeating
-- itself (issue #64).
--
-- key: one per thing looked at —
--   'query:<search query>'      a seed-hunt search (rotated by checked_at)
--   'site:<host>'               an entity's website mined for its calendar
--   'domain:<host>'             a host events' source_url point at, mined
--   'url:<canonical url>'       a calendar URL verified (unreachable ones wait)
--   'memory'                    the last Codex-from-memory proposal run
--   'term:<kind>:<term>'        an organisation-harvest search term (rotation)
-- outcome: what came of it ('proposed', 'no-calendar', 'unreachable',
-- 'known-seed', 'searched', …); detail: JSON evidence.
-- A proposed seed itself lives in event_seeds (any status is remembered
-- there); this table only spares the next run the same Firecrawl calls.

CREATE TABLE IF NOT EXISTS discovery_checks (
  key         TEXT PRIMARY KEY,
  checked_at  TEXT NOT NULL,
  outcome     TEXT,
  detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_discovery_checks_at ON discovery_checks(checked_at);
