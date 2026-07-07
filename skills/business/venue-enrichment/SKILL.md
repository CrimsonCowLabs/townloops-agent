---
name: venue-enrichment
description: Fill in address, phone, website, description, and image for venues mined from event sources. Runs on cron 09:00 daily; can be invoked ad-hoc when the queue is deep.
---

# venue-enrichment

Consumes `enrichment_queue` rows where `kind='venue'`. For each, fetches the venue's website (or source URL) and extracts og:image, JSON-LD address/phone, description. Fills BLANK fields only — never overwrites operator-edited data.

## Commands

```
python skills/business/venue-enrichment/enrich.py sweep [--limit N] [--firecrawl]
python skills/business/venue-enrichment/enrich.py ship  [--batch N]
python skills/business/venue-enrichment/enrich.py depth
```

- `sweep` — claim up to N queued rows, research each, mark ready.
- `ship` — PATCH ready rows back to TownLoops.
- `depth` — how many unclaimed venue rows are waiting.

## Rhythm

Cron 09:00 daily runs `sweep --limit 20`, then the next `event-ingest ship` picks up the readies. Both run within the enrichment-sweep cron job.

## Env

- `TOWNLOOPS_API_URL`, `TOWNLOOPS_API_KEY`, `TOWNLOOPS_TOWN_SLUG`
- `FIRECRAWL_API_KEY` (optional; only used with `--firecrawl`)

## Fills

`description`, `address`, `website`, `phone`, `image_path`, `image_alt`. All fills are additive; if the local row already has a value, the enricher leaves it alone.
