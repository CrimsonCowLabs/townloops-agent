-- Phase B+: store the actual Townloops slug per row so PATCH paths
-- survive Townloops-side slug rewrites (e.g. when the admin renames an
-- event or Townloops generates auto-slugs that diverge from our
-- deterministic hermes-<hash> form).
--
-- After this migration, every successful POST/PATCH captures the slug
-- from the response body and writes it here. Old rows whose slugs we
-- never captured get backfilled by `events.py resync`.

ALTER TABLE events       ADD COLUMN townloops_slug TEXT;
ALTER TABLE venues       ADD COLUMN townloops_slug TEXT;
ALTER TABLE businesses   ADD COLUMN townloops_slug TEXT;
ALTER TABLE communities  ADD COLUMN townloops_slug TEXT;
ALTER TABLE performers   ADD COLUMN townloops_slug TEXT;
ALTER TABLE creators     ADD COLUMN townloops_slug TEXT;
