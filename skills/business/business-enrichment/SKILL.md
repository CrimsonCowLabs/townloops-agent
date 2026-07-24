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
python skills/business/business-enrichment/enrich.py cross-check [--town-hint "Town Name"]
```

## Fills

`description`, `address`, `website`, `phone`, `image_path`, `image_alt`, and socials
(`instagram`, `facebook`, `tiktok`, `twitter`, `youtube`, `linkedin`) — all only when
the local row's column is currently NULL. Operator edits are never overwritten.

## `cross-check` — peek at CMO's unshipped drafts

Discovery of new businesses lives on the CMO Hermes, not here. `cross-check` reads the CMO's
`townloops-research/data/drafts.db` (path via `$TOWNLOOPS_RESEARCH_DRAFTS_DB`, read-only) and lists
unshipped business drafts. Use it to know what the CMO has queued up for TownLoops before
you notice new rows appearing in the local `businesses` table.

Does NOT ship — shipping happens on the CMO side via `research.py ship`. See
`docs/business-discovery-handoff.md` for the full handoff.
