-- 016_community_enrichment_status.sql
-- communities never got the enrichment_status / enriched_at pair that
-- venues/businesses/performers have (005). event-reconcile records
-- 'enriched' | 'not-found' | 'live' | 'image-proposed' there so a community
-- with no findable image isn't re-researched every week.

ALTER TABLE communities ADD COLUMN enrichment_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE communities ADD COLUMN enriched_at TEXT;

CREATE INDEX IF NOT EXISTS idx_communities_enrich ON communities(enrichment_status);
