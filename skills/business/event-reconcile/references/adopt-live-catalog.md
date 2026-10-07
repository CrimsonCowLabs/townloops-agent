# Adopting an Existing Live Catalog Locally

Use this when a town already has published TownLoops events but its local event-state database is empty or incomplete. This is a **local bootstrap/re-adoption**, not an ingest or remote write.

## Required outcome

Remote published events become terminal local rows:

- `approval_status='approved'`
- `state='shipped'`
- `townloops_slug` populated
- `pushed_at` and `pushed_hash` populated
- no unresolved `event_workqueue` row

This prevents the event-first pipeline from presenting live records for approval or POSTing them again.

## Safe sequence

1. Require `TOWNLOOPS_TOWN_SLUG`, API URL, and bearer. Refuse any API row whose `town_slug` differs from the configured town.
2. Fetch published events with pagination. For an active-catalog bootstrap, start at the town-local current date. Fetch venues too, and map `venue_slug` to the canonical venue name.
3. Run a dry comparison before writes. Match in this order:
   1. exact `townloops_slug`;
   2. exact canonical `source_url` plus town-local day;
   3. normalized title plus town-local day plus venue;
   4. normalized title plus town-local day only when exactly one candidate exists.
4. Back up `state/townloops.db`.
5. In one `BEGIN IMMEDIATE` transaction, update matched rows and insert unmatched live rows. Keep unmatched pending discoveries unchanged.
6. Resolve any open workqueue rows for adopted event UIDs. Do not enqueue adopted rows.
7. Commit, then verify invariants below.

## Field mapping

- Generate stable new `event_uid` values from `townloops-live:{town_slug}:{remote_id}`.
- Convert `start_at` and `end_at` to both UTC and town-local columns. Derive `date_day` from local time, not by slicing the UTC timestamp.
- Preserve original local provenance on a matched row where useful, but set `discovered_via='townloops-api'` and record remote ID/slug/import time/match method in `extra_json.townloops_import`.
- Set `source='townloops-api'` and `source_kind='live'` for newly mirrored rows.
- Compute a stable content hash from the remote fields and use it for both `content_hash` and `pushed_hash`.
- Store categories and tags from the live record. Do not invent missing data.

## Verification queries

All must be zero after adoption:

- imported rows with `approval_status != 'approved'`;
- imported rows missing `pushed_at` or `pushed_hash`;
- imported rows missing `townloops_slug`;
- duplicate non-null `townloops_slug` values;
- unresolved workqueue rows attached to adopted UIDs.

Also run `operator-status` and list a sample with `operator-list-events --status shipped`.

## API route pitfall

OpenAPI paths include `/api/v1`. Profiles may configure `TOWNLOOPS_API_URL` as either the host root or the versioned base. Normalize the base before appending `/towns/{slug}/...`; otherwise reads incorrectly hit `/towns/...` and return 404.

## Scope cautions

- This procedure mirrors existing remote state locally; it must never PATCH or POST TownLoops.
- Do not mark unmatched local pending discoveries as shipped.
- A date filter can include an occurrence whose UTC date differs from its town-local day. Always derive local display/state fields from timezone-aware timestamps.
