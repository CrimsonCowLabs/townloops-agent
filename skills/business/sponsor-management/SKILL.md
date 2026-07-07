---
name: sponsor-management
description: Manage sponsors and ad-spot placements for the town. Gated by SPONSORS_ENABLED. When disabled, keeps a local roster + placement schedule; when enabled, also syncs to the TownLoops sponsor/ad_spot endpoints (see docs/api-sponsor-schema-proposal.md). Money involved — ALWAYS ask before add or assign.
---

# sponsor-management

## Status: partially blocked

The TownLoops v1 Ingest API has no sponsor / ad-spot endpoints. This skill runs against the proposed schema at `docs/api-sponsor-schema-proposal.md`. Behavior:

| `SPONSORS_ENABLED` | What runs |
|---|---|
| `false` (default) | local `sponsors` + `ad_spots` tables only. Safe to build the roster ahead of API landing. Newsletter template still reads `ad_spots` for header/footer if the flag is on. |
| `true` | same local writes, plus `POST /api/v1/towns/<slug>/sponsors` and `POST /api/v1/towns/<slug>/ad_spots`. |

Flip the flag in `.env` when TownLoops ships the endpoints and you've minted an ingest key with sponsor:write scope.

## Commands

```
python skills/business/sponsor-management/sponsors.py add-sponsor \
    --name "Alibi Ale Works" --kind business --tier gold \
    --website https://alibialeworks.com --logo-url https://.../logo.png \
    --starts-at 2026-07-14

python skills/business/sponsor-management/sponsors.py list-sponsors

python skills/business/sponsor-management/sponsors.py assign \
    --sponsor <sponsor_uid|slug> --placement newsletter-header \
    --asset https://.../banner.png --click https://sponsor.example \
    --alt "Grand opening" --starts-at 2026-07-14 --ends-at 2026-10-31 --weight 3

python skills/business/sponsor-management/sponsors.py audit
```

## The Monday audit

`10:00 Monday` cron runs `sponsors.py audit` and reports:
- Ad spots expiring in the next 14 days
- Sponsors with no active ad spots
- (When enabled) Tier over-allocation warnings

Output goes to the operator's Telegram thread as a plain-text digest.

## MUST ask before

- `add-sponsor` — every new record represents a paying relationship
- `assign` — creates a placement obligation
- ANY change while `SPONSORS_ENABLED=true` (real writes to TownLoops)

The audit is safe to run autonomously.

## Env

- `SPONSORS_ENABLED` (default `false`)
- `TOWNLOOPS_API_URL`, `TOWNLOOPS_API_KEY`, `TOWNLOOPS_TOWN_SLUG` (only used when enabled)
