-- 029_ingest_ref.sql
-- The Ingest Ref (#63, townloops ADR 0011): the agent's own stable ID for a
-- row it ships, `hermes-<town>-<uid16>` (engine/ingest_ref.ref_for).
--
-- ingest_ref: the ref the live row is KNOWN to carry — set when a create
-- carrying it succeeds, when a 409 ingest_ref_taken binds to an existing row,
-- or when a lookup / the adoption sweep confirms or sets it (set-once PATCH).
-- NULL on a shipped row = shipped before refs, not yet adopted.

ALTER TABLE events ADD COLUMN ingest_ref TEXT;
ALTER TABLE venues ADD COLUMN ingest_ref TEXT;
ALTER TABLE businesses ADD COLUMN ingest_ref TEXT;
ALTER TABLE communities ADD COLUMN ingest_ref TEXT;
ALTER TABLE performers ADD COLUMN ingest_ref TEXT;
ALTER TABLE creators ADD COLUMN ingest_ref TEXT;
