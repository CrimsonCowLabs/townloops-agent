-- 033_event_detail_description.sql
-- What an event's own page says it is, for events whose listing or adapter
-- row gave no description (engine/event_detail): sentences copied from the
-- detail page, each quote-checked against it. Kept apart from `description`
-- — the listing's text, which the content hash covers and every re-read
-- rewrites — so a re-read never loses it and never reads as a change.
-- event-ship sends it only while `description` is blank
-- (engine/event_quality.description).

ALTER TABLE events ADD COLUMN detail_description TEXT;
