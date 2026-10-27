---
name: sponsor-management
description: Manage sponsors and ad-slot placements for the town. Gated by SPONSORS_ENABLED. When disabled, keeps a local roster + placement schedule; when enabled, also syncs to the TownLoops sponsor/ad-slot endpoints (/towns/{slug}/sponsors and /towns/{slug}/ad-slots per openapi.json). Money involved — ALWAYS ask before add or assign.
---

# sponsor-management

## Status: live

The TownLoops v1 API now ships sponsor + ad-slot endpoints (see `openapi.json` — `SponsorPayload` and `AdSlotPayload`). This skill talks to them directly. `docs/api-sponsor-schema-proposal.md` is superseded by the live schema and kept only for history.

| `SPONSORS_ENABLED` | What runs |
|---|---|
| `false` (default) | Local `sponsors` + `ad_spots` tables only. Safe to build a roster ahead of a sponsor go-live. Newsletter template still reads `ad_spots` for header/footer. |
| `true` | Same local writes, plus `POST /api/v1/towns/<slug>/sponsors` and `POST /api/v1/towns/<slug>/ad-slots`. |

Flip the flag in `.env` after minting an ingest key with sponsor:write scope.

## Payload shapes (matches openapi.json)

**Sponsor** (the ad creative itself):

```json
{
  "name": "Alibi Ale Works",           // REQUIRED
  "image_alt": "Alibi Ale Works logo", // REQUIRED — accessibility mandate (NOT NULL server-side)
  "slug": "alibi-ale-works",           // optional; auto-slugified if omitted
  "blurb": "Craft brewery on Ski Run Blvd.",
  "image_url": "https://.../logo.png", // server fetches through SSRF-guarded pipeline → image_path
  "image_path": "images/foo.png",      // when both set, image_path wins (explicit beats derived)
  "link_url": "https://alibialeworks.com",
  "is_active": true
}
```

**AdSlot** (a scheduled placement of a sponsor):

```json
{
  "placement": "homepage",             // enum: homepage|newsletter|category|featured_event|sidebar
  "sponsor_slug": "alibi-ale-works",   // must match an existing sponsor's slug
  "starts_at": "2026-07-14T00:00:00Z", // REQUIRED — natural key: (placement, sponsor_slug, starts_at)
  "ends_at": "2026-10-31T23:59:59Z",   // optional
  "priority": 100,                     // 0..1000, default 0
  "is_active": true
}
```

Reschedule = DELETE + POST — `starts_at` is intentionally not on `AdSlotPatch`.

## Local → API field mapping

The local DB kept the older column names (predates the API landing). Translation happens at POST time in `sponsors.py`.

| Local column (`sponsors`) | API field | Notes |
|---|---|---|
| `name` | `name` | |
| `logo_url` | `image_url` | Renamed. |
| `description` | `blurb` | Renamed. |
| `website` | `link_url` | Renamed. |
| `image_alt` | `image_alt` | **Added in migration 006. Required by API.** |
| `is_active` | `is_active` | |
| `townloops_slug` | `slug` | Sent on POST if we already know it. |
| `kind`, `tier`, `contact_email`, `starts_at`, `ends_at` | *(not sent)* | Local classification / reporting only. |

| Local column (`ad_spots`) | API field | Notes |
|---|---|---|
| `placement` | `placement` | **Enum changed** — see below. |
| `sponsor_uid` (join → `sponsors.townloops_slug`) | `sponsor_slug` | Sponsor must be pushed first. |
| `starts_at` | `starts_at` | REQUIRED for remote POST. |
| `ends_at` | `ends_at` | |
| `weight` | `priority` | Renamed; `0..1000`. |
| `asset_url`, `click_url`, `alt_text` | *(not sent)* | These moved to the sponsor record. Archived locally for history. |

**Placement enum change**: earlier drafts used `newsletter-header` / `newsletter-footer` / `homepage-hero` / `event-detail-banner` / `sidebar`. The API now accepts only `homepage` / `newsletter` / `category` / `featured_event` / `sidebar`. The CLI enforces the new set at input.

## Commands

```
# Note: --image-alt is REQUIRED (accessibility mandate on the API).
$PYTHON $HERMES_HOME/skills/business/sponsor-management/sponsors.py add-sponsor \
    --name "Alibi Ale Works" --kind business --tier gold \
    --link-url https://alibialeworks.com \
    --image-url https://.../logo.png \
    --image-alt "Alibi Ale Works logo — pint glass over Lake Tahoe" \
    --blurb "Craft brewery on Ski Run Blvd."

$PYTHON $HERMES_HOME/skills/business/sponsor-management/sponsors.py list-sponsors

# --starts-at is REQUIRED for ad-slots (server's natural-key de-dup).
$PYTHON $HERMES_HOME/skills/business/sponsor-management/sponsors.py assign \
    --sponsor <sponsor_uid|slug> \
    --placement homepage \
    --starts-at 2026-07-14T00:00:00Z --ends-at 2026-10-31T23:59:59Z \
    --priority 100

$PYTHON $HERMES_HOME/skills/business/sponsor-management/sponsors.py audit
```

## The Monday audit

`10:00 Monday` cron runs `sponsors.py audit` and reports:
- Ad-slots expiring in the next 14 days
- Sponsors with no active ad-slots
- Sponsors missing `image_alt` (would be rejected on push)

Output goes to the operator's Telegram thread as a plain-text digest.

## MUST ask before

- `add-sponsor` — every new record represents a paying relationship
- `assign` — creates a placement obligation
- ANY change while `SPONSORS_ENABLED=true` (real writes to TownLoops)

The audit is safe to run autonomously.

## Env

- `SPONSORS_ENABLED` (default `false`)
- `TOWNLOOPS_API_URL`, `TOWNLOOPS_API_KEY`, `TOWNLOOPS_TOWN_SLUG` (only used when enabled)
