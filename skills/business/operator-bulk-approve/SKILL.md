# operator-bulk-approve

**When to use:** the operator asks to approve multiple pending events at once — "approve everything from the North Tahoe Chamber", "approve the four community meetings I just reviewed".

**READ-ONLY.** This skill *proposes* a bulk approval; it does not execute one. Execution happens via the dashboard's Confirm widget — see [operator-bulk-decline](../operator-bulk-decline/SKILL.md) for the full "why Hermes can't mutate" story. Confirmed approvals also enqueue the first pipeline action (`read-detail`) for each uid, mirroring the dashboard's per-row approve button.

## CLI

```
approve.py --source "north tahoe chamber"
approve.py --before 2026-08-15
approve.py --source "palisades" --limit 10
```

Filters (at least one required):

- `--before`  ISO date/datetime
- `--source`  substring match on `discovered_via` (case-insensitive)
- `--limit`   1..500 (default 500)

## Output shape

```json
{
  "proposal": {
    "kind": "approve_events",
    "filters": {"before": null, "source": "chamber"},
    "count": 5,
    "uids": ["…", "…", …]
  },
  "preview": [{"event_uid": "…", "title": "…", "date": "…", "source": "…"}, …]
}
```

## Autonomous mode

Inactive: answers `{"inactive": true, …}` — the agent approves events itself
and TownLoops moderation is the review step (`autonomy why <id>` explains a call).
