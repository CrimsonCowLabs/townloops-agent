-- 026_listing_website_clears.sql
-- The listing-website cleanup (engine/listing_website_cleanup.py): one row
-- per entity whose listing-page `website` (engine/listing_hosts) is being or
-- was cleared, local and live.
--
-- target: '<kind>:<entity_uid>', or '<kind>#<live id>' for a live row no
-- local row pairs with. status: 'confirmed' (supervised: the operator
-- confirmed it, the next apply clears it) | 'submitted' (autonomous: a
-- pending copy without the website is in the TownLoops merge flow,
-- pending_slug) | 'cleared' | 'failed' (the live clear didn't land; parked
-- until confirmed again) | 'not-accepted' (the moderator didn't take the
-- clear) | 'resolved' (no listing website any more when applied).
-- changed: what the clear changed — 'live', 'local', 'both' or 'none'.
-- reenrich: 'queued' until enrichment has looked for the entity's own site,
-- then its outcome ('enriched', 'not-found', ...), or 'skipped' (why in note).
-- url: the listing URL cleared (live's when both held one); the entity also
-- keeps it as research evidence (extra_json.listing_pages).

CREATE TABLE IF NOT EXISTS listing_website_clears (
  target        TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  entity_uid    TEXT,
  live_slug     TEXT,
  url           TEXT,
  status        TEXT NOT NULL,
  changed       TEXT,
  pending_slug  TEXT,
  reenrich      TEXT,
  note          TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listing_website_clears_status ON listing_website_clears(status);
CREATE INDEX IF NOT EXISTS idx_listing_website_clears_pending ON listing_website_clears(pending_slug);
