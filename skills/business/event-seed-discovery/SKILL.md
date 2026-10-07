---
name: event-seed-discovery
description: Propose, verify, and stage event-source URLs for operator approval.
---

# event-seed-discovery

Uses the LLM (Codex CLI) to propose seed source URLs for a town — chamber
of commerce, city government, county government, library, tourism board,
parks & rec, local newspaper, and major venues. These become the starting
points for the daily `event-find` cron.

Runs weekly (Sunday 04:00 UTC) and on first-run from `town-init`. New
proposals land as `event_seeds.status='proposed'` for the operator to
approve, reject, or edit in the dashboard's Seeds panel.

## CLI

```bash
python skills/business/event-seed-discovery/discover.py discover
python skills/business/event-seed-discovery/discover.py discover --limit N
python skills/business/event-seed-discovery/discover.py discover --dry-run
python skills/business/event-seed-discovery/discover.py show
```

`discover` runs LLM discovery, HEAD-verifies each URL, and inserts survivors
as proposed seeds. It is idempotent and skips URLs already in `event_seeds`.
`show` prints current seed rows as JSON lines.

## Prompt design

Given the town name, aliases, and region, the prompt asks Codex CLI for a
compact JSON array of seed candidates with `name`, `url`, and `kind`.
Kinds are constrained to chamber, government, tourism, library, parks,
newspaper, venue, or other.

## URL verification

Every LLM-proposed URL is HEAD-checked via Firecrawl. A 2xx response is required for initial staging, but it is not evidence that the URL is a usable automated source. Branded 404 pages, challenge pages, empty JavaScript widgets, and vendor error responses can all return 2xx.

Before moving a `manual_only` seed to automated operation, run the source-automation review in `event-find`: establish a dry-run baseline on a copied database, inspect raw and rendered content, identify stable JSON/iCal/vendor endpoints, verify pagination and geographic scope, and classify error pages as failed runs rather than successful empty runs. Prefer replacing a broken seed URL with the current canonical calendar URL instead of teaching extraction around a branded error page.

## Auto-approve gate

Nothing is auto-approved. The operator reviews proposals in the dashboard.
This is intentional: a bad seed can create substantial review noise.
