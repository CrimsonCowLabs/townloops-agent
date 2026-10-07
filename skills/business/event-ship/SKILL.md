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
