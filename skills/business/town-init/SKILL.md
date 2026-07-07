---
name: town-init
description: Bootstrap this deployment for its town — verify TownLoops + Listmonk credentials, snapshot the platform category taxonomy, and migrate the state SQLite. Use once when the repo is first deployed for a town, then rerun `status` any time to see counts.
---

# town-init

One-shot bootstrap + status dashboard for the town this agent operates.

## When to use

- **First boot after cloning the repo for a new town.** Run `setup` to verify credentials and initialize state.
- **Anytime you want the numbers.** Run `status` to print live counts of events/venues/businesses + enrichment queue depth + last newsletter.
- Cron does NOT invoke this. It is on-demand.

## Environment

- `TOWNLOOPS_API_URL`, `TOWNLOOPS_API_KEY`, `TOWNLOOPS_TOWN_SLUG` (required)
- `LISTMONK_URL`, `LISTMONK_USER`, `LISTMONK_TOKEN` (optional at setup — a warning if unreachable, not a hard failure)

## Commands

```
python skills/business/town-init/init.py setup
python skills/business/town-init/init.py status
```

### `setup`
1. `GET /api/v1/healthz` — reachable?
2. `GET /api/v1/towns/<slug>/venues?limit=1` — does the bearer token authorize the configured town?
3. Best-effort `GET /api/lists` on Listmonk — confirms newsletter creds.
4. Snapshot `GET /api/v1/towns/<slug>/categories` → `state/categories.json`.
5. Apply pending SQL migrations from `/app/engine/migrations/`.

Exit code 0 on success, 2 on hard credential failure. Prints a JSON summary to stdout.

### `status`
- Live counts from TownLoops (paginated) per resource kind.
- Enrichment queue depth per kind.
- Last-newsletter row from local state DB.

Run this any time you want a snapshot of the town's data health.
