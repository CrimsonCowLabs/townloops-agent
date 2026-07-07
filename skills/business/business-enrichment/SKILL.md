---
name: business-enrichment
description: Fill in address, phone, website, description, and image for businesses. Seeded from venue research (venues that are also businesses) and organizers mined during event ingest. Runs on cron 09:00 daily.
---

# business-enrichment

Same shape as `venue-enrichment` but for the `businesses` table. Seeded by:

- Event-ingest mining: iCal `ORGANIZER`, JSON-LD `organizer`.
- Venue-enrichment side-effects: some venues also register as businesses (see `docs/community-to-business-audit.md`).
- Manual: user pastes a URL → agent inserts via `event-ingest add-manual` + `enqueue`.

## Commands

```
python skills/business/business-enrichment/enrich.py sweep [--limit N] [--firecrawl]
python skills/business/business-enrichment/enrich.py ship  [--batch N]
python skills/business/business-enrichment/enrich.py depth
```

## Fills

`description`, `address`, `website`, `phone`, `image_path`, `image_alt`.
