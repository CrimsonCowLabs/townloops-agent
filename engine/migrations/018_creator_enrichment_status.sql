-- 018_creator_enrichment_status.sql
-- creators were the last entity table without enrichment_status /
-- enriched_at. event-enrich-entities records 'enriched' | 'not-found' there
-- so an entity that was already researched isn't looked up again, and a
-- not-found is a recorded outcome rather than a silent pass-through.

ALTER TABLE creators ADD COLUMN enrichment_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE creators ADD COLUMN enriched_at TEXT;

CREATE INDEX IF NOT EXISTS idx_creators_enrich ON creators(enrichment_status);
