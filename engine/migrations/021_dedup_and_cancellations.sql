-- 021_dedup_and_cancellations.sql
-- Cross-source dedup and cancellation detection (engine/event_dedup.py,
-- engine/event_cancel.py).
--
-- event_seeds.source_role: 'primary' | 'syndicator', set by the operator.
-- NULL means "work it out" — known aggregator hosts and newspaper/tourism
-- seeds count as syndicators, everything else as primary.
--
-- events.missed_finds: consecutive successful event-find runs of the row's
-- seed that covered its date and didn't list it. Reset when seen again.
-- events.cancel_status: NULL | 'possibly-cancelled' (flagged for the
-- operator) | 'confirmed' (operator said cancelled; not yet live) |
-- 'cancelled' (marked cancelled on TownLoops) | 'apply-failed' (the live
-- marking didn't land; operator re-confirms) | 'dismissed' (operator said
-- it's still on — never re-flagged).

ALTER TABLE event_seeds ADD COLUMN source_role TEXT;
ALTER TABLE events ADD COLUMN missed_finds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN cancel_status TEXT;
ALTER TABLE events ADD COLUMN cancel_flagged_at TEXT;
ALTER TABLE events ADD COLUMN cancel_note TEXT;

CREATE INDEX IF NOT EXISTS idx_events_cancel ON events(cancel_status) WHERE cancel_status IS NOT NULL;
