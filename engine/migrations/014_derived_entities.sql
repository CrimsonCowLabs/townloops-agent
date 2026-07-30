-- 014_derived_entities.sql
-- Entities are now derived from approved events, not independently discovered.
-- Add provenance columns to existing entity tables and a many-to-many linked_events.

ALTER TABLE businesses  ADD COLUMN derived_from_event_uid TEXT;
ALTER TABLE businesses  ADD COLUMN derivation_confidence INTEGER;
ALTER TABLE businesses  ADD COLUMN linked_event_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE venues      ADD COLUMN derived_from_event_uid TEXT;
ALTER TABLE venues      ADD COLUMN derivation_confidence INTEGER;
ALTER TABLE venues      ADD COLUMN linked_event_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE communities ADD COLUMN derived_from_event_uid TEXT;
ALTER TABLE communities ADD COLUMN derivation_confidence INTEGER;
ALTER TABLE communities ADD COLUMN linked_event_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE performers  ADD COLUMN derived_from_event_uid TEXT;
ALTER TABLE performers  ADD COLUMN derivation_confidence INTEGER;
ALTER TABLE performers  ADD COLUMN linked_event_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE creators    ADD COLUMN derived_from_event_uid TEXT;
ALTER TABLE creators    ADD COLUMN derivation_confidence INTEGER;
ALTER TABLE creators    ADD COLUMN linked_event_count INTEGER NOT NULL DEFAULT 0;

-- Many-to-many: entities host many events; events may reference many entities
CREATE TABLE IF NOT EXISTS linked_events (
  link_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uid     TEXT NOT NULL,
  entity_kind   TEXT NOT NULL,      -- 'business' | 'community' | 'venue' | 'performer' | 'creator'
  entity_uid    TEXT NOT NULL,
  role          TEXT NOT NULL,      -- 'host' | 'venue' | 'performer' | 'creator' | 'sponsor'
  linked_at     TEXT NOT NULL,
  UNIQUE (event_uid, entity_kind, entity_uid, role)
);

CREATE INDEX IF NOT EXISTS idx_linked_event ON linked_events(event_uid);
CREATE INDEX IF NOT EXISTS idx_linked_entity ON linked_events(entity_kind, entity_uid);
