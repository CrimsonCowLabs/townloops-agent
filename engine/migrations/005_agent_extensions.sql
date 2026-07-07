-- townloops-agent-specific extensions on top of the SLT schema.
-- Adds enrichment queue, sponsor/ad_spot mirrors, newsletter run log.

-- Enrichment tracking on entity rows.
ALTER TABLE venues       ADD COLUMN enrichment_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE venues       ADD COLUMN enriched_at TEXT;
ALTER TABLE businesses   ADD COLUMN enrichment_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE businesses   ADD COLUMN enriched_at TEXT;
ALTER TABLE performers   ADD COLUMN enrichment_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE performers   ADD COLUMN enriched_at TEXT;

CREATE INDEX IF NOT EXISTS idx_venues_enrich ON venues(enrichment_status);
CREATE INDEX IF NOT EXISTS idx_businesses_enrich ON businesses(enrichment_status);
CREATE INDEX IF NOT EXISTS idx_performers_enrich ON performers(enrichment_status);

-- Polymorphic queue driving enrichment sweeps.
CREATE TABLE IF NOT EXISTS enrichment_queue (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL,           -- 'venue' | 'business' | 'performer'
  entity_uid   TEXT NOT NULL,
  queued_at    TEXT NOT NULL,
  claimed_by   TEXT,
  claimed_at   TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT,
  resolved_at  TEXT,
  UNIQUE (kind, entity_uid)
);
CREATE INDEX IF NOT EXISTS idx_queue_unclaimed
  ON enrichment_queue(kind, queued_at)
  WHERE claimed_at IS NULL AND resolved_at IS NULL;

-- Sponsor & ad_spot mirrors — populated locally, shipped when SPONSORS_ENABLED=true.
CREATE TABLE IF NOT EXISTS sponsors (
  sponsor_uid    TEXT PRIMARY KEY,       -- sha1(name_norm)
  name           TEXT NOT NULL,
  kind           TEXT NOT NULL,          -- 'business' | 'community'
  logo_url       TEXT,
  website        TEXT,
  description    TEXT,
  contact_email  TEXT,
  tier           TEXT,                   -- 'gold' | 'silver' | 'bronze' | 'community'
  starts_at      TEXT,
  ends_at        TEXT,
  is_active      INTEGER NOT NULL DEFAULT 1,
  content_hash   TEXT NOT NULL,
  townloops_slug TEXT,
  pushed_at      TEXT,
  pushed_hash    TEXT
);

CREATE TABLE IF NOT EXISTS ad_spots (
  ad_spot_uid    TEXT PRIMARY KEY,       -- sha1(sponsor_uid|placement|starts_at)
  sponsor_uid    TEXT NOT NULL REFERENCES sponsors(sponsor_uid),
  placement      TEXT NOT NULL,          -- 'newsletter-header' | ... (see docs/api-sponsor-schema-proposal.md)
  asset_url      TEXT NOT NULL,
  click_url      TEXT NOT NULL,
  alt_text       TEXT,
  starts_at      TEXT,
  ends_at        TEXT,
  weight         INTEGER NOT NULL DEFAULT 1,
  content_hash   TEXT NOT NULL,
  townloops_id   TEXT,
  pushed_at      TEXT,
  pushed_hash    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ad_spots_placement ON ad_spots(placement, starts_at);

-- Weekly newsletter log.
CREATE TABLE IF NOT EXISTS newsletters (
  iso_week           TEXT PRIMARY KEY,   -- '2026-W28'
  composed_at        TEXT,
  event_count        INTEGER,
  body_path          TEXT,               -- state/newsletters/<iso-week>.html
  pushed_at          TEXT,
  campaign_id        INTEGER,
  scheduled_send_at  TEXT,
  status             TEXT NOT NULL DEFAULT 'draft'  -- 'draft' | 'scheduled' | 'sent' | 'failed'
);
