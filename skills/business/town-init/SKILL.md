---
name: town-init
description: Bootstrap this deployment for its town — snapshot the platform category taxonomy, migrate the state SQLite, and run preflight (one read-only verdict on whether the town will work). Use `setup` once when a town is first deployed, `preflight` any time something seems wrong (the first diagnostic), and `status` for live counts.
---

# town-init

One-shot bootstrap + status dashboard for the town this agent operates.

## When to use

- **First boot after cloning the repo for a new town.** Run `setup` to initialize state; it ends with preflight.
- **Before `docker compose up`, and first whenever something stops working.** Run `preflight`.
- **Anytime you want the numbers.** Run `status` to print live counts of events/venues/businesses + enrichment queue depth + last newsletter.
- Cron does NOT invoke this. It is on-demand.

## Environment

- `TOWNLOOPS_API_URL`, `TOWNLOOPS_API_KEY`, `TOWNLOOPS_TOWN_SLUG` (required)
- `LISTMONK_URL`, `LISTMONK_USER`, `LISTMONK_TOKEN` (optional at setup — a warning if unreachable, not a hard failure)

## Commands

```
python skills/business/town-init/init.py setup
python skills/business/town-init/init.py preflight [--json]
python skills/business/town-init/init.py status
```

### `setup`
1. Snapshot `GET /api/v1/towns/<slug>/categories` → `state/categories.json` (best-effort; a bad key shows up in preflight).
2. Apply pending SQL migrations from `/app/engine/migrations/`.
3. Run preflight.

Prints a JSON summary to stdout (`findings`, `ok`) and preflight's human report to stderr. Exit 0 when
preflight has no errors, 1 otherwise — `ok: true` means the town will work, not just that setup ran.

### `preflight`
Read-only and cheap (`engine/preflight.py`): no writes, no probe-by-create, no pagination, the state DB
opened read-only. Each finding is a severity (`error` = the agent cannot work, `warning` = it works with a
piece missing, `info` = can't be checked), a stable `code`, the file + key it concerns, and the
consequence. Exit 0 with no errors, 1 with any — warnings never fail. `--json` prints
`{"ok", "findings": [{severity, code, where, message, detail}]}`.

Checks: TownLoops healthz + one-row read + `verify_town()` (`api-unreachable`, `credential-rejected`,
`town-scope`, `credential-missing`); the profile (`profile-missing`, `profile-placeholder`,
`profile-unverifiable` info for `name`/`region`); every `discovery.community.query_groups[].category_slug`
against the snapshot (`category-unknown` lists the known slugs, `category-snapshot-stale` when live has it
— re-run `setup`; `categories-snapshot-missing`); operating mode (`dashboard-password-missing` supervised,
`dashboard-enabled-autonomous` when `docker-compose.override.yml` doesn't park the dashboard,
`dashboard-unverified` when the town folder isn't visible — set `TOWNLOOPS_TOWN_DIR`); an open autonomy
circuit (`circuit-open`, reason + evidence verbatim — the publish-key case included; `state-db-missing`);
Firecrawl (`firecrawl-not-configured` incl. cloud, `firecrawl-unreachable`); `codex-login-missing`;
`AGENT_TZ` (`tz-invalid`, `tz-fallback`); `listmonk-unconfigured` / `listmonk-unreachable` and, autonomous
only, `telegram-missing` (warnings).

### `status`
- Live counts from TownLoops (paginated) per resource kind.
- Enrichment queue depth per kind.
- Last-newsletter row from local state DB.

Run this any time you want a snapshot of the town's data health.
