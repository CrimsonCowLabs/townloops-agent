# event-ship

Workqueue action: **ship**. Runs for events in
`state=entities-enriched`.

POSTs derived entities FIRST, then the event, to TownLoops. Ordering
matters — the event references venue by slug, so venue must exist
first. Per plan adversarial pass #19: venue → host → performers →
creators → event.

## Contract

```
ship.py --event-uid <uid>
```

Exit codes:
- `0` — event POSTed successfully (or was already-shipped), `events.pushed_at` + `events.townloops_slug` set.
- `1` — TownLoops API returned a non-recoverable error (401/403/404 on town endpoint, malformed payload). Orchestrator retries up to 3 times then marks stuck.
- `2` — event not found in local DB.

## Idempotency

Each POST uses `Idempotency-Key: hermes-{town_slug}:ship:{entity_kind}:{entity_uid}`.
Re-shipping the same event on retry is safe — TownLoops returns 200
with the existing record.

If TownLoops returns `409 slug_taken`, we treat it as already-shipped
success (this is the same convention `event-ingest ship` uses at
`ingest.py:668`).

## Times are UTC only

`start_at` / `end_at` come from the `date_utc` / `end_at_utc` columns, which
`engine.event_extract.upsert_event` fills from the extracted local times in the
town timezone (`$AGENT_TZ`). A row queued before those columns were set is
converted the same way at ship time. An event with no UTC start is **not
shipped**: ship records a failed `ship-event` activity and exits 1, so the
workqueue retries and then marks it `stuck-` for the operator. A naive local
date is never sent; an end time with no UTC form is left off.

## Categories and tags

The event goes out with `category_slugs` (all of its categories) and `tags`;
TownLoops' create schema has no singular `category_slug`. Categories are the
row's `category_slugs` (or legacy `category_slug`) mapped onto the town's live
slugs in `state/categories.json` (`engine/categories.py`) — old extraction
vocabulary like `music`/`food` becomes `live-music`/`food-drink`, and anything
that maps to no live slug is dropped. Tags are sent as slugs, which is what the
API stores and returns.

A create path has been seen to drop `tags` (CMO, bulk create). So when the
created row lacks a category or tag set that was sent, ship follows with a
full-body PATCH (`engine.live_patch.apply_event_patch`) and GET-verifies it.
If the sets still don't stick, the event's live slug is recorded, a failed
`verify-live-event` activity is logged, the event is parked `stuck-` for the
operator, and ship exits 1. A `409 slug_taken` create (a retry, or an
operator requeue after a failed repair) GETs the live row and runs the same
check. With no `state/categories.json` snapshot, events ship without
categories and ship warns on stderr — run `town-init setup`.

## When TownLoops isn't ready yet

If `TOWNLOOPS_API_KEY` env is missing or empty, this skill logs the
event as "shipping disabled" and exits 0. The event state advances to
`shipped` in the local DB even though nothing was POSTed. This lets
the operator visualize the whole pipeline running end-to-end during
town onboarding, before TownLoops.com even has the town record.

When the town is finally registered on TownLoops, an operator-triggered
`resync` will POST the accumulated shipped-locally events.

## Reconcile-staged rows

- An event whose `extra_json.reconcile.kind == "changed"` (staged by
  `event-reconcile`) is a source-side change to an existing live event: ship
  **PATCHes** `events/<live_slug>` with the changed `start_at`/`end_at` and/or
  `venue_name` + `venue_id` (live mirror) or `venue_slug` (venue shipped in
  step 1), instead of POSTing a new event. On a date change with no usable
  source end time, the live end is shifted by the same amount as the start;
  an end before the start is never sent (rows staged without the live times
  read them with a GET first). A venue change whose new venue has no live id
  or slug yet fails with rc 1 for a workqueue retry rather than PATCHing only
  the name. Once a date-change PATCH lands, the older local row for the old
  date is marked `status='superseded'` and loses its `townloops_slug`.
- Linked entities with `source_kind='live'` are local mirrors of TownLoops rows;
  ship returns their `townloops_slug` without POSTing.
