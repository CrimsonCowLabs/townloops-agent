-- 019_discovery_live_slug.sql
-- business-discovery dedups each candidate against the businesses already
-- live on TownLoops. A match is stored as status 'already-live' with the
-- live business's slug here, so the operator sees what it matched and the
-- candidate is never proposed again.

ALTER TABLE discovery_candidates ADD COLUMN live_slug TEXT;
