-- 009_entity_harvest_state.sql
-- ISO timestamp of the last event-source-harvest pass over each entity.
-- Used by the harvest skill to skip re-probing the same URLs on a 7-day
-- cooldown. Nullable — never harvested = NULL = probe on next pass.

ALTER TABLE businesses  ADD COLUMN last_harvested_at TEXT;
ALTER TABLE venues      ADD COLUMN last_harvested_at TEXT;
ALTER TABLE communities ADD COLUMN last_harvested_at TEXT;
