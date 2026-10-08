-- 022_seed_feeds_and_health.sql
-- Feed probing (engine/feed_probe.py) and source-health alerts
-- (engine/source_health.py) on event_seeds.
--
-- feed_type / feed_url: the best machine-readable feed event-seed-discovery
-- found on the seed's page ('ical', 'google-calendar', 'tribe',
-- 'eventcalendarapp', 'point', 'jsonld', 'rss', 'html-calendar', or
-- 'wix-calendar-wrapper' with no url). NULL type after a probe = nothing found.
-- feed_detail: JSON of everything the probe found (plus resolved_url when
-- the seed URL redirected). feed_probed_at: NULL = never probed.
--
-- health_status: NULL (fine) | 'zero' | 'drop' — the seed's latest
-- successful event-find listed nothing, or far fewer than its trailing
-- average (health_baseline). Held until the count recovers or the operator
-- accepts the new level (health_since: only runs started after it count).

ALTER TABLE event_seeds ADD COLUMN feed_type TEXT;
ALTER TABLE event_seeds ADD COLUMN feed_url TEXT;
ALTER TABLE event_seeds ADD COLUMN feed_detail TEXT;
ALTER TABLE event_seeds ADD COLUMN feed_probed_at TEXT;
ALTER TABLE event_seeds ADD COLUMN health_status TEXT;
ALTER TABLE event_seeds ADD COLUMN health_note TEXT;
ALTER TABLE event_seeds ADD COLUMN health_baseline REAL;
ALTER TABLE event_seeds ADD COLUMN health_flagged_at TEXT;
ALTER TABLE event_seeds ADD COLUMN health_since TEXT;
