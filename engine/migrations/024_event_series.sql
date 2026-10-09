-- 024_event_series.sql
-- The TownLoops Series (ADR 0007) each live event belongs to, mirrored from
-- the Event resource's `series_slug` / `series_id`. Written only from what
-- TownLoops returns — event-ship's create/adopt, event-reconcile's live
-- snapshot, entity-live-sync's re-read — never inferred locally. NULL = a
-- singleton (or not read yet). A slug may name an archived Series: it stays
-- a grouping key, never a link target. See engine/series.py.

ALTER TABLE events ADD COLUMN series_slug TEXT;
ALTER TABLE events ADD COLUMN series_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_events_series_slug ON events(series_slug);
