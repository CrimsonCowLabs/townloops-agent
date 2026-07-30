-- 010_events_hidden.sql
-- Operator can hide an event via the dashboard so it doesn't display.
-- Separate from `events.status` because event-ingest's _upsert_event
-- rewrites `status` on every UPDATE — a shared column would clobber
-- operator hides on the next fetch. `hidden` is dashboard-owned; ingest
-- reads it (to preserve on UPDATE) but never writes it.

ALTER TABLE events ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_events_visible ON events(date_day) WHERE hidden = 0;
