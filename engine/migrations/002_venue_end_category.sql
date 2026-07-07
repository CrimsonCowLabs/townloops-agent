-- Add fields needed for richer Townloops EventPayload mapping.
-- All three default to NULL; existing rows keep their content_hash unchanged
-- on this DDL — the new fields get backfilled on the next `ingest`, which
-- will flip content_hash for affected rows and trigger PATCH-shipping.
ALTER TABLE events ADD COLUMN venue_name TEXT;
ALTER TABLE events ADD COLUMN end_at TEXT;
ALTER TABLE events ADD COLUMN category_slug TEXT;
