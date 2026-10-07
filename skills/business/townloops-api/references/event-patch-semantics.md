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

## Before a POST: look up live (ticket 06)

`GET /events` filters only by `status`/`from`/`to`/`category` (no
`source_url`, `limit` ≤ 200, deleted rows excluded, pending included), so an
existing event is found by slug GET, then by `source_url` among the events
listed in a ±12h window. `POST` answers a taken slug with `409 slug_taken`,
which only says *a* row has that slug — GET it before calling it shipped.
On create, the venue goes by `venue_id`: `_resolve_venue` uses
`scalar_one_or_none` on a case-insensitive in-town name match, so a name
shared by two in-town venues errors with "Multiple rows were found".
`event-ship` does all of this via `engine/live_lookup.py`.

## Entity PATCH (venues, businesses, communities, performers, creators) — ticket 09

`update_venue` & co. are a plain per-field merge: `None`/omitted leaves a
field alone and an empty `name` is ignored, but any other value — **`""`
included — overwrites**, and a `category_slug` that doesn't resolve
**clears** the category (with a `category_not_found` warning). No field
replaces a set. `engine/live_patch.apply_entity_patch` sends the live row's
own non-blank values plus the fills (within the API's length limits, never
blanks) and GET-verifies; `engine/entity_sync.py` only ever fills fields
that are blank live, and only sends a `category_slug` from the town's
category snapshot.
