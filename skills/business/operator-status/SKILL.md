# operator-status

**When to use:** the operator asks about the health of the town's event pipeline in general terms — "what's the state of things?", "how many pending events?", "any events stuck?", "how many seeds have I approved?".

Returns a compact JSON snapshot of the same headline chips shown on the dashboard: pending / approved / declined events, in-flight through the enrichment pipeline, shipped to TownLoops, seed counts by status, and workqueue depth. Read-only; no side effects.

Prefer this over `operator-list-events` when the operator wants *counts* rather than specific event details.

## CLI

```
status.py                     # emit JSON on stdout
```

## Output shape

```json
{
  "town": "north-shore",
  "generated_at": "2026-07-29T…",
  "events": {"pending": 123, "approved": 1, "declined": 0, "shipped": 1},
  "seeds":  {"proposed": 3, "approved": 24, "rejected": 4},
  "workqueue": {"active": 0, "stuck": 0, "by_action": {…}},
  "in_flight_events": 0
}
```
