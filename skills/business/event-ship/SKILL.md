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
