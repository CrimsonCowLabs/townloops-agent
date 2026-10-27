---
name: townloops-api
description: Snapshot the TownLoops OpenAPI spec, detect breaking changes and freeze the affected resources for write operations, describe endpoints (filters / ordering / pagination), and issue general-purpose read queries. Daily cron `api-check` keeps the snapshot fresh.
---

# townloops-api

Owns the agent's contract-of-record with TownLoops's REST API. Three concerns:

1. **Snapshot** the live OpenAPI JSON to `state/openapi.json` (with previous
   at `state/openapi.previous.json`).
2. **Detect breaking changes** — removed paths, removed operations, removed
   required fields, changed property types. Freezes affected resources by
   writing `state/api_freeze.json`, which `engine/townloops_api.Client`
   consults before every POST/PATCH/DELETE.
3. **Query** — general-purpose paginated + filtered + ordered GETs. Use for
   ad-hoc reads (dashboards, audits, migrations); ingest/enrichment continue
   to use their own client methods for writes.

## Commands

```
$PYTHON $HERMES_HOME/skills/business/townloops-api/api.py snapshot [--url URL]
$PYTHON $HERMES_HOME/skills/business/townloops-api/api.py diff [--against PATH]
$PYTHON $HERMES_HOME/skills/business/townloops-api/api.py check [--url URL]
$PYTHON $HERMES_HOME/skills/business/townloops-api/api.py describe <resource>
$PYTHON $HERMES_HOME/skills/business/townloops-api/api.py query <resource> \
    [--filter K=V ...] [--from ISO] [--to ISO] [--order-by KEY] \
    [--limit N] [--offset N] [--all]
```

## Live event-listing workflow

For audience-facing requests such as “what’s on?” or “what’s happening this weekend?”:

1. Read the current local time with `date`; report times in the town timezone.
2. Run `describe events` before constructing filters when the snapshot or contract may have changed.
3. Query the **live** API, normally with `--filter status=published`, a bounded `--from` / `--to` window, and a reasonable limit. Do not substitute the local operator review queue; older-pipeline records may exist only in TownLoops.
4. To include events already in progress, start the API window at the beginning of the local day (or earlier for overnight events), then retain rows whose `end_at` is absent or later than now. A query beginning exactly at “now” can omit ongoing events because filtering is based on `start_at`.
5. Convert UTC timestamps to the town timezone, group by local day, and flag obvious catalog conflicts rather than silently presenting them (for example, source/title mismatch or two events assigned to the same room at the same time).
6. Keep broad future catalogs out of the response. Summarize the requested date range and state the count.

### API base-path pitfall

The query helper builds `TOWNLOOPS_API_URL + /towns/{slug}/{resource}`. Therefore `TOWNLOOPS_API_URL` must include the API prefix, currently `/api/v1`. If the profile sets only the origin (for example `https://townloops.com`), override it for the command:

```bash
TOWNLOOPS_API_URL=https://townloops.com/api/v1 \
  $PYTHON $HERMES_HOME/skills/business/townloops-api/api.py query events \
  --filter status=published --from 2026-10-06 --to 2026-10-12 --all
```

A 404 whose URL is `/towns/<slug>/...` without `/api/v1` indicates this base-path mismatch. Confirm the current route with `describe`; do not infer that the town has no records.

- `snapshot` — fetch and save; emits one-line JSON status.
- `diff` — structured diff of current vs. previous snapshot; distinguishes
  breaking from non-breaking; maps schema/path changes to affected resources.
- `check` — the cron entrypoint. Snapshot → diff previous vs new → if breaking
  changes, write freeze + Telegram alert. Additive-only changes update the
  snapshot silently.
- `describe` — reads the current snapshot and prints the exact
  filter/order/pagination parameters the resource's endpoints accept.
  Reference before writing a `query` invocation.
- `query` — paginated GET on any resource. `--all` follows pagination; the
  cap is 200 pages so a runaway filter can't hang. Time-range endpoints use
  `--from` / `--to`, which become the API's `from` / `to` query params.

## Editing live events (PATCH)

PATCH merges per field, but `category_slugs`/`tags` replace the whole set, an
unresolved `venue_name` clears the venue, and `venue_slug` is silently ignored.
Never hand-build a partial PATCH: use `engine.live_patch.apply_event_patch`
(full body from the current live row + GET verify). Details:
`references/event-patch-semantics.md`.

## Freeze semantics

`state/api_freeze.json`:

```json
{
  "frozen_at": "2026-07-19T12:00:00+00:00",
  "reason": "breaking change vs abc123def456",
  "resources": ["events", "businesses"]
}
```

`engine/townloops_api.Client` reads this on every write. A frozen resource
raises `TownloopsError(code='resource_frozen')` on POST/PATCH/DELETE. Reads
(`list`, `get`, `paginate`) are not gated — surveying a frozen resource is
still fine.

To clear a freeze after updating payload builders: delete
`state/api_freeze.json` (or remove the offending resources from its `resources`
array). The next `check` run will re-populate it if the breakage persists.

## Series contract

TownLoops Series (ADR 0007) arrived additively — `GET /series`,
`GET /series/{slug}`, `series_slug`/`series_id` on the Event resource, the
`?series=` filter — and the diff reads that as non-breaking. `check` also
enforces what `engine/series.py` relies on (`series_contract`), and exits 1
with a Telegram alert when:

- a Series path gains a write operation (POST/PUT/PATCH/DELETE) — drift to
  know about; the client refuses Series writes regardless
  (`READ_ONLY_RESOURCES`);
- a request schema (`*Payload`/`*Patch`/`*Entry`) can set an Event's Series
  — **freezes `events`**;
- `EventResource.series_slug` disappears — **freezes `events`**.

`check` also refreshes the local Series snapshot `state/series.json` (active
Series: slug, title, cadence, active flag), best-effort.

## Rhythm

Daily cron entry `api-check` (04:15 UTC) calls `check`. On breakage:
Telegram alert with resource list + Swagger link; freeze persists until
operator clears it. On additive changes: silent snapshot update.

## Env

- `TOWNLOOPS_OPENAPI_URL` — default `https://townloops.com/api/v1/openapi.json`.
- `TOWNLOOPS_SWAGGER_URL` — default `https://townloops.com/api/v1/docs`.
- `TOWNLOOPS_API_URL`, `TOWNLOOPS_API_KEY`, `TOWNLOOPS_TOWN_SLUG` —
  used by `query` and by `Client` writes. `TOWNLOOPS_API_URL` may be either the
  host root (`https://townloops.com`) or already include `/api/v1`; the query
  helper must normalize both forms and must not construct bare `/towns/...`
  routes from a host-root value.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — optional; enables `check` alerts.

## Non-goals

- Not for writing to the API. Use ingest / enrichment CLIs, which go through
  `engine/townloops_api.Client` and honor freeze state.
- Not a schema validator. Payload correctness is enforced by the API at POST
  time; this skill only tracks *shape drift* over time.
- Not an OpenAPI code generator. Snapshot is JSON on disk; consumers read it
  directly.
