# operator-list-events

**When to use:** the operator asks to see *which* events match a filter — "list pending events", "show me events from the chamber source", "what's coming up before Aug 15?", "which events are declined?".

Returns a JSON list of events matching the filter, one row per event, capped by `--limit` (default 20, max 100). Read-only; wraps `db_v2.events_by_approval`. Prefer `operator-status` for pure counts, and `operator-review-event` (v2, when it lands) for a single-event deep dive.

## CLI

```
list.py                                    # 20 pending events (default)
list.py --status approved                  # approved events
list.py --status pending --limit 5         # first 5 pending
list.py --status pending --before 2026-08-15
                                            # pending, starting before Aug 15
list.py --source "chamber"                 # substring match on discovered_via
```

`--status`  ∈  {pending, approved, declined, shipped}  (default: pending)
`--before`  ISO date/datetime (e.g. `2026-08-15` or `2026-08-15T12:00`)
`--source`  case-insensitive substring match on `discovered_via`
`--limit`   1-100 (default 20)

## Output shape

```json
{
  "status": "pending",
  "count": 3,
  "events": [
    {"event_uid": "…", "title": "…", "date": "…", "venue": "…",
     "source": "…", "category": "…"},
    …
  ]
}
```

If zero events match, `count: 0` and `events: []` — not an error.
