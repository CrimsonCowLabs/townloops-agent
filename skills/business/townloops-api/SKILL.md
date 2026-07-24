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
python skills/business/townloops-api/api.py snapshot [--url URL]
python skills/business/townloops-api/api.py diff [--against PATH]
python skills/business/townloops-api/api.py check [--url URL]
python skills/business/townloops-api/api.py describe <resource>
python skills/business/townloops-api/api.py query <resource> \
    [--filter K=V ...] [--from ISO] [--to ISO] [--order-by KEY] \
    [--limit N] [--offset N] [--all]
```

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

## Rhythm

Daily cron entry `api-check` (04:15 UTC) calls `check`. On breakage:
Telegram alert with resource list + Swagger link; freeze persists until
operator clears it. On additive changes: silent snapshot update.

## Env

- `TOWNLOOPS_OPENAPI_URL` — default `https://townloops.com/api/v1/openapi.json`.
- `TOWNLOOPS_SWAGGER_URL` — default `https://townloops.com/api/v1/docs`.
- `TOWNLOOPS_API_URL`, `TOWNLOOPS_API_KEY`, `TOWNLOOPS_TOWN_SLUG` —
  used by `query` and by `Client` writes.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — optional; enables `check` alerts.

## Non-goals

- Not for writing to the API. Use ingest / enrichment CLIs, which go through
  `engine/townloops_api.Client` and honor freeze state.
- Not a schema validator. Payload correctness is enforced by the API at POST
  time; this skill only tracks *shape drift* over time.
- Not an OpenAPI code generator. Snapshot is JSON on disk; consumers read it
  directly.
