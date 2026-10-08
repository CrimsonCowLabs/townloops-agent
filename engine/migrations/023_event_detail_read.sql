-- 023_event_detail_read.sql
-- The per-event detail-page pass (engine/event_detail.py, the
-- `event-read-detail` skill on the `read-detail` workqueue action).
--
-- accessibility_notes: the event page's own words about access (wheelchair,
-- ASL, sensory-friendly, seating, parking…), quote-checked against the page.
-- NULL = the page says nothing. Ships as the live event's accessibility_notes.
--
-- detail_read_status: NULL (not read yet) | 'read' | 'no-detail-page' (no
-- source_url, or it is the seed's listing page) | 'failed' (gave up on the
-- last workqueue attempt; read again on the event's next pass).
-- detail_read_url: the source_url that pass was for — a new source_url is
-- read again. detail_read_at: when.

ALTER TABLE events ADD COLUMN accessibility_notes TEXT;
ALTER TABLE events ADD COLUMN detail_read_status TEXT;
ALTER TABLE events ADD COLUMN detail_read_url TEXT;
ALTER TABLE events ADD COLUMN detail_read_at TEXT;
