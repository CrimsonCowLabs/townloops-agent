# TownLoops event PATCH semantics

Established 2026-10-07 (ticket 03) by reading the API source — townloops repo
`app/schemas/api.py:EventPatch`, `app/api/v1.py:patch_event_route` /
`_patch_to_input`, `app/services/events.py:update_event` / `_resolve_venue` —
and cross-checked against the live OpenAPI snapshot (`state/openapi.json`,
`EventPatch` properties identical). No live writes were made to establish this.

## Finding: per-field MERGE, with three replace/clear traps

`PATCH /api/v1/towns/{town}/events/{slug}` is a **merge**: a field that is
omitted or `null` is left alone. But:

1. **`category_slugs` and `tags` replace the whole set.** `[]` clears all; a
   one-item list drops every other category/tag. Always send the full set.
2. **`venue_name` that doesn't resolve CLEARS the venue** (`venue_id` → NULL,
   name kept as `unresolved_venue_name`, `venue_not_found` warning). Name
   lookup is case-insensitive in-town, then global only if exactly one match.
   `venue_id`, when sent, takes precedence over `venue_name` — and is resolved
   **in-town only**, so resending the `venue_id` of an event bound to another
   town's venue (e.g. Reno) clears it. Send `venue_id` only to change venue.
3. **Unknown keys are silently dropped** (pydantic default). Notably there is
   **no `venue_slug` on PATCH or POST** — sending it does nothing. Resolve the
   slug to an id (`GET /venues/{slug}` → `id`) and send `venue_id`.

Other notes:
- `slug` is not patchable. `business_id: 0` clears the business link; it is
  not in the event GET resource, so it can't round-trip.
- Naive datetimes are read as town-local; send UTC with an offset.
- PATCH does **not** validate `end_at` against `start_at` (only POST does) —
  never send an end before the start.
- `null` cannot clear a field (e.g. an `end_at`); only an operator edit can.

## Why the CMO saw a blanked title

The API once mapped a missing title to `""` (`p.title or ""`) in
`_patch_to_input`, so any PATCH without `title` blanked it. That is fixed in
the API source, but don't depend on it: deployments can lag and the
replace/clear traps above still apply.

## Rule (enforced in code)

Every edit to a live event goes through `engine/live_patch.py`:
`apply_event_patch(client, slug, overrides)` GETs the live row, sends every
round-trippable field it has (full `category_slugs`/`tags`; not `venue_id`,
see above; not a value the API would 422, like a <3-char title) with the
local changes on top, then GETs again and returns any field that didn't land
as sent, plus a live end left before the start. `event-ship` then parks the
event (`workqueue.park`: `stuck-` + queue rows resolved) for the operator, so
nothing re-PATCHes it automatically. An empty live row or a time override
without a UTC offset raises instead of patching.
