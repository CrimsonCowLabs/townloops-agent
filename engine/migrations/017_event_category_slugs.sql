-- Events carry several categories plus free tags, both as JSON arrays of
-- slugs (TownLoops `category_slugs[]` / `tags[]`). Rows written by extraction
-- hold live slugs; backfilled rows may hold old vocabulary until ship maps it.
-- `category_slug` stays as the primary (first) category for older readers.
-- Ported from the CMO's south-lake-tahoe-events migration 005.
ALTER TABLE events ADD COLUMN category_slugs TEXT;
ALTER TABLE events ADD COLUMN tags TEXT;

-- Backfill single-category rows. Values from the old extraction vocabulary
-- (`music`, `food`, …) are mapped onto live slugs at ship time.
UPDATE events
SET category_slugs = json_array(trim(category_slug))
WHERE category_slugs IS NULL
  AND category_slug IS NOT NULL
  AND trim(category_slug) != '';
