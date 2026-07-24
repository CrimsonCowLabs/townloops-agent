-- The TownLoops API's Business/Community/Performer/Creator payloads gained
-- social-media + image fields at some point after migration 003 was written.
-- Enrichment could scrape them but had nowhere to store them, so we were
-- silently dropping data before ship. This migration closes that gap.
--
-- Field names match the OpenAPI payload contracts exactly (see
-- openapi.json → components.schemas.{Business,Community,Performer,Creator}Payload).
--
-- Notes:
--   * Business + Community both use `youtube` (bare); Performer likewise.
--     Creator uses `youtube_url` — a pre-existing quirk of the API.
--   * All new columns are nullable; enrichment fills only what it can verify.

ALTER TABLE businesses ADD COLUMN image_path TEXT;
ALTER TABLE businesses ADD COLUMN image_alt  TEXT;
ALTER TABLE businesses ADD COLUMN instagram  TEXT;
ALTER TABLE businesses ADD COLUMN facebook   TEXT;
ALTER TABLE businesses ADD COLUMN tiktok     TEXT;
ALTER TABLE businesses ADD COLUMN twitter    TEXT;
ALTER TABLE businesses ADD COLUMN youtube    TEXT;
ALTER TABLE businesses ADD COLUMN linkedin   TEXT;

ALTER TABLE communities ADD COLUMN image_path TEXT;
ALTER TABLE communities ADD COLUMN image_alt  TEXT;
ALTER TABLE communities ADD COLUMN instagram  TEXT;
ALTER TABLE communities ADD COLUMN facebook   TEXT;
ALTER TABLE communities ADD COLUMN tiktok     TEXT;
ALTER TABLE communities ADD COLUMN twitter    TEXT;
ALTER TABLE communities ADD COLUMN youtube    TEXT;
ALTER TABLE communities ADD COLUMN linkedin   TEXT;

ALTER TABLE performers ADD COLUMN facebook TEXT;
ALTER TABLE performers ADD COLUMN tiktok   TEXT;
ALTER TABLE performers ADD COLUMN twitter  TEXT;
ALTER TABLE performers ADD COLUMN youtube  TEXT;
ALTER TABLE performers ADD COLUMN linkedin TEXT;

ALTER TABLE creators ADD COLUMN facebook TEXT;
ALTER TABLE creators ADD COLUMN tiktok   TEXT;
ALTER TABLE creators ADD COLUMN twitter  TEXT;
ALTER TABLE creators ADD COLUMN linkedin TEXT;
