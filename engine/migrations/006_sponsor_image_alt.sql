-- Landed sponsor + ad-slot endpoints on the TownLoops API introduced new
-- required and renamed fields. This migration aligns the local `sponsors`
-- table with what we now POST.
--
-- - image_alt is required by the API (NOT NULL on the server) as an
--   accessibility mandate. We keep it nullable locally so half-authored
--   rows can sit in the DB before push, but sponsors.py refuses to POST
--   without it.
-- - The rest of the API-side rename dance (logo_url→image_url,
--   description→blurb, website→link_url, weight→priority) is handled
--   in-code at POST time so we don't churn every local row on a rename.

ALTER TABLE sponsors ADD COLUMN image_alt TEXT;
