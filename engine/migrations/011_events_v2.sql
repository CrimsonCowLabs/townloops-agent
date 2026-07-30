-- 011_events_v2.sql
-- Event-first pipeline: events are the atomic unit; entities derived post-approval.
-- Adds workqueue state, approval status, canonical dedup, dual-column timezones,
-- affinity/confidence scoring. Retains existing columns (name, date, description,
-- hosting_group, etc.) for backward compatibility with newsletter/ship code paths.

-- Approval + workqueue state
ALTER TABLE events ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE events ADD COLUMN approval_note TEXT;
ALTER TABLE events ADD COLUMN approval_at TEXT;
ALTER TABLE events ADD COLUMN state TEXT NOT NULL DEFAULT 'found';
ALTER TABLE events ADD COLUMN state_updated_at TEXT;

-- Dedup: canonical event UID groups source-scoped duplicates
ALTER TABLE events ADD COLUMN canonical_event_uid TEXT;

-- Confidence + provenance
ALTER TABLE events ADD COLUMN metadata_confidence INTEGER;
ALTER TABLE events ADD COLUMN in_town_confidence INTEGER;
ALTER TABLE events ADD COLUMN discovered_via TEXT;

-- Richer metadata columns beyond the initial schema
ALTER TABLE events ADD COLUMN title TEXT;               -- may differ from `name` after operator edit
ALTER TABLE events ADD COLUMN venue_raw TEXT;
ALTER TABLE events ADD COLUMN location_raw TEXT;
ALTER TABLE events ADD COLUMN image_url TEXT;
ALTER TABLE events ADD COLUMN image_local_path TEXT;    -- filled by extract-metadata after download
ALTER TABLE events ADD COLUMN price TEXT;
ALTER TABLE events ADD COLUMN rsvp_url TEXT;
ALTER TABLE events ADD COLUMN tags_json TEXT;           -- JSON array of tags

-- Dual-column timezones — display uses local, ship uses utc
ALTER TABLE events ADD COLUMN date_utc TEXT;
ALTER TABLE events ADD COLUMN date_local TEXT;
ALTER TABLE events ADD COLUMN end_at_utc TEXT;
ALTER TABLE events ADD COLUMN end_at_local TEXT;

-- Derived entity links (single-value FKs; many-to-many in `linked_events`)
ALTER TABLE events ADD COLUMN derived_venue_uid TEXT;
ALTER TABLE events ADD COLUMN derived_host_uid TEXT;
ALTER TABLE events ADD COLUMN derived_host_kind TEXT;   -- 'business' or 'community'

CREATE INDEX IF NOT EXISTS idx_events_state ON events(state);
CREATE INDEX IF NOT EXISTS idx_events_approval ON events(approval_status);
CREATE INDEX IF NOT EXISTS idx_events_canonical ON events(canonical_event_uid);
