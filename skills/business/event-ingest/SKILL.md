---
name: event-ingest
description: Aggregate community events from configured sources, dedupe, mine venue/business/community/performer/creator stubs, and ship to TownLoops. Use when the user says "collect events", "ingest events", "ship to TownLoops", "resync", or when scheduled cron fires event-collect-{am,pm}.
---

# event-ingest

Fork of the SLT events aggregator, town-agnostic. Reads a per-town `sources.yaml`, parses each source with a structure-aware parser (iCal / JSON-LD / RSS / HTML selectors / Firecrawl), normalizes events, dedupes, upserts to SQLite, and ships deltas to `POST/PATCH /api/v1/towns/<slug>/{events,venues,businesses,communities,performers,creators}`.

## When to use

- **Cron fires event-collect-am (06:00) or event-collect-pm (18:00)** — runs `collect` + `ship` end-to-end.
- User says "collect", "ingest", "gather events", "run the pipeline", "ship deltas".
- After new sources are added to `sources.yaml` — an ad-hoc collect proves them.
- After enrichment marks entities ready — an ad-hoc ship pushes the PATCH round.

## Environment (required)

- `TOWNLOOPS_API_URL`, `TOWNLOOPS_API_KEY`, `TOWNLOOPS_TOWN_SLUG`
- Optional: `SOURCES_YAML_PATH` (default `$HERMES_HOME/state/sources.yaml`), `TOWNLOOPS_DB_PATH` (default `$HERMES_HOME/state/townloops.db`), `AGENT_TZ` (default `America/Los_Angeles`), `FIRECRAWL_API_KEY` (optional; needed only for firecrawl-typed sources).

## Commands

```
python skills/business/event-ingest/ingest.py <subcommand> [flags]
```

| Subcommand | What it does |
|---|---|
| `ingest` | fetch every configured source, dedupe, upsert to SQLite. NO API writes. |
| `ship` | POST new + PATCH changed events + mined entities. Batches (100/req). |
| `ship --dry-run` | print payloads, don't POST. Use for smoke tests. |
| `ship --only-kind venue,performer` | limit ship to those kinds. |
| `resync` | reconcile local `townloops_slug` with the remote. Cures slug drift. |
| `resync --delete-orphans` | delete remote rows whose local origin is gone. Destructive; ask before running. |
| `kinds` | print row counts + unpushed counts per kind. Sanity check. |
| `list --limit 50` | dump upcoming events from local SQLite. |
| `add-manual` / `import-manual` | inject events without a source (readings, private data). |
| `fetch` | one-shot dump to stdout, no DB writes. Debug. |
| `list-sources` | print the parsed `sources.yaml`. |
| `validate` | hit every source, report counts, no writes. Use after editing sources.yaml. |

## The daily rhythm

Cron chains `ingest` → `ship`:

```
python skills/business/event-ingest/ingest.py ingest \
  && python skills/business/event-ingest/ingest.py ship
```

`ingest` writes into `state/townloops.db`; `ship` reads unpushed rows and posts them. Mining side-effects (venue/business/community/performer rows) also get enqueued into `enrichment_queue` for the enrichment skills to pick up on the 09:00 sweep.

## Content-hash + slug capture

Every row carries `content_hash` (input to dedupe) and `townloops_slug` (captured from the POST response). PATCH paths use the captured slug so remote renames don't strand rows. When slug drift is suspected (rare), run `resync` to reconcile.

## When it goes wrong

- **`FATAL: TOWNLOOPS_TOWN_SLUG env-var is required`** — .env didn't load. Confirm the container has `env_file: .env` and that the file exists.
- **All sources return 0 events** — check `sources.yaml` freshness (`state/sources.yaml`); calendars change URLs seasonally.
- **PATCH 404s** — the remote entity was deleted. Run `resync` to clear the stale `townloops_slug`, then re-ship.

## Depends on

- `engine/migrations/*.sql` — schema (mounted at `/app/engine/migrations`).
- Python packages: `feedparser`, `requests`, `pyyaml`, `beautifulsoup4`, `python-dateutil`, `icalendar`.
