# Proposed TownLoops API endpoints — sponsors & ad_spots

**Status:** proposal. Not implemented in TownLoops v1.

The `sponsor-management` skill is written against this shape and gated by `SPONSORS_ENABLED=false`. Flip the flag once the endpoints land and mint an ingest key with sponsor write scope.

## Bearer scope

Add two new permissions: `sponsors:write`, `ad_spots:write`. Existing town × ingest tokens gain them opt-in via `/admin/api-keys` (do NOT auto-grant to old tokens).

## `POST /api/v1/towns/{town_slug}/sponsors`

Create a sponsor. Slug auto-derived from name unless supplied.

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | display name |
| `slug` | string | no | url-safe; server generates if missing |
| `kind` | enum(`business`,`community`) | yes | |
| `logo_url` | url | no | ideally 200×200+ PNG |
| `website` | url | no | |
| `description` | text | no | ≤ 500 chars |
| `contact_email` | string | no | |
| `tier` | enum(`gold`,`silver`,`bronze`,`community`) | yes | drives placement priority |
| `starts_at` | ISO datetime | no | default now |
| `ends_at` | ISO datetime | no | null = open-ended |
| `is_active` | bool | no | default true |

Response: `{data: {sponsor: {...}, slug: "..."}, warnings: []}`.

## `GET/PATCH/DELETE /api/v1/towns/{town_slug}/sponsors/{sponsor_slug}`

Standard CRUD.

## `POST /api/v1/towns/{town_slug}/ad_spots`

Bind a sponsor to a placement over a time window.

| Field | Type | Required | Notes |
|---|---|---|---|
| `sponsor_id` | int | yes | (or `sponsor_slug`) |
| `placement` | enum | yes | see below |
| `asset_url` | url | yes | image to display |
| `click_url` | url | yes | href on click |
| `alt_text` | string | no | |
| `starts_at` | ISO datetime | no | default now |
| `ends_at` | ISO datetime | no | |
| `weight` | int | no | default 1; higher = more likely when placements rotate |

### `placement` enum

- `newsletter-header` — top of weekly newsletter
- `newsletter-footer` — bottom of weekly newsletter
- `homepage-hero` — town landing page hero
- `event-detail-banner` — inside individual event pages
- `sidebar` — sidebar on category listing pages

Response: `{data: {ad_spot: {...}, id: 123}, warnings: []}`.

## `GET /api/v1/towns/{town_slug}/ad_spots`

Query params: `placement`, `active_at` (ISO — returns spots active at that instant), `sponsor_id`.

## `GET/PATCH/DELETE /api/v1/towns/{town_slug}/ad_spots/{ad_spot_id}`

Standard CRUD.

## `POST /api/v1/towns/{town_slug}/ad_spots/rotate` (optional)

Server-side weighted-random pick for a placement. Body: `{placement: "newsletter-header"}`. Response: single ad_spot or 204 if none active.

## Server-side validation

- `starts_at < ends_at` when both set.
- One tier=gold sponsor gets a soft cap of 3 active `newsletter-header` spots at a time; enforce with 409 + `{code: "tier_slot_limit"}`.
- `asset_url` must respond 200 to a HEAD from the API server (defense against dead links). Warn, not fail.

## What the agent needs

Once these ship, the agent's `sponsor-management` skill:

1. `sponsors.py add-sponsor --name "Alibi Ale Works" --tier gold --website ...` → local + POST.
2. `sponsors.py assign --sponsor alibi-ale-works --placement newsletter-header --asset ... --click ... --starts 2026-07-14 --ends 2026-12-31 --weight 3`
3. `sponsors.py list-active [--placement ...]` → GET filtered.
4. `sponsors.py audit` → weekly cron: flags spots expiring < 14d, sponsors with no active spots, tier-slot-limit warnings.
