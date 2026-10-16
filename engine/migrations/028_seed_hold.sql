-- 028_seed_hold.sql
-- Seed maintenance verbs (event-seed-discovery move / hold / release).
--
-- event_seeds.status gains 'held': out of service on purpose — event-find
-- never scrapes it and the autonomous seed review never trials, re-approves,
-- relocates or retires it, until `release`.
-- hold_reason: why it is held — 'manual-only' | 'awaiting-adapter' (NULL
-- unless held); the free-text note goes in status_note.

ALTER TABLE event_seeds ADD COLUMN hold_reason TEXT;
