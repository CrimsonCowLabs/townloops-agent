---
name: event-ingest
description: Operate the legacy sources.yaml aggregator or reconcile its locally tracked rows. For current event discovery and staging, use event-seed-discovery followed by event-find; use event-ship only after operator approval.
---

# event-ingest

## Pipeline status

This is the **legacy `sources.yaml` aggregation pipeline**, retained for compatibility and maintenance of rows it already owns. It is not the default event-discovery path.

The current event-first pipeline is:

1. `event-seed-discovery` proposes source URLs for operator review.
2. The operator approves seeds in the dashboard.
3. `event-find` scrapes approved seeds and stages extracted events as `metadata-extracted` + `approval_status=pending`.
4. The operator approves or declines staged events.
5. Workqueue skills derive/enrich entities, then `event-ship` publishes approved events.

Do not ship pending seed-pipeline events through this legacy skill, and do not treat an empty `sources.yaml` result as evidence that the seed pipeline has no events.

This skill reads a per-town `sources.yaml`, parses each source with a structure-aware parser (iCal / JSON-LD / RSS / HTML selectors / Firecrawl), normalizes events, dedupes, upserts to SQLite, and can ship deltas for legacy-owned rows to `POST/PATCH /api/v1/towns/<slug>/{events,venues,businesses,communities,performers,creators}`.

## When to use

- Maintaining or diagnosing an existing legacy `sources.yaml` source.
- Running `validate`, `list-sources`, `kinds`, or `resync` for legacy-owned rows.
- Shipping deltas that are explicitly known to belong to this legacy pipeline.

For requests such as “find events,” “review sources,” “collect current events,” or “get events into staging,” use `event-seed-discovery` and `event-find` instead.

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
| `resync --delete-orphans` | Destructive remote deletion mode. Out of scope for this agent; do not run. |
| `kinds` | print row counts + unpushed counts per kind. Sanity check. |
| `list --limit 50` | dump upcoming events from local SQLite. |
| `add-manual` / `import-manual` | inject events without a source (readings, private data). |
| `fetch` | one-shot dump to stdout, no DB writes. Debug. |
| `list-sources` | print the parsed `sources.yaml`. |
| `validate` | hit every source, report counts, no writes. Use after editing sources.yaml. |

## Legacy execution pattern

The legacy pipeline can be run manually as:

```bash
python skills/business/event-ingest/ingest.py ingest \
  && python skills/business/event-ingest/ingest.py ship
```

Do not use that chain as the default town event job. The active event-first scheduler runs seed discovery, `event-find`, operator review, workqueue derivation/enrichment, and `event-ship`. Legacy `ingest` writes legacy-owned rows into `state/townloops.db`; legacy `ship` reads and publishes their deltas.

## Content-hash + slug capture

Every row carries `content_hash` (input to dedupe) and `townloops_slug` (captured from the POST response). PATCH paths use the captured slug so remote renames don't strand rows. When slug drift is suspected (rare), run `resync` to reconcile.

## When it goes wrong

- **Remote TownLoops already has a live catalog but local event-state rows are missing** — `resync` is not a catalog importer; it only reconciles slugs for rows already present locally. Use `event-reconcile/references/adopt-live-catalog.md` to mirror remote events as terminal `approved` + `shipped` rows without reposting them.
- **`FATAL: TOWNLOOPS_TOWN_SLUG env-var is required`** — `.env` didn't load. Confirm the container has `env_file: .env` and that the file exists.
- **All sources return 0 events** — check `sources.yaml` freshness (`state/sources.yaml`); calendars change URLs seasonally.
- **PATCH 404s** — the remote entity was deleted. Run `resync` to clear the stale `townloops_slug`, then re-ship.

## Depends on

- `engine/migrations/*.sql` — schema (mounted at `/app/engine/migrations`).
- Python packages: `feedparser`, `requests`, `pyyaml`, `beautifulsoup4`, `python-dateutil`, `icalendar`.
