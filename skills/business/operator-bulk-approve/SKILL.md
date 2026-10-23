# operator-bulk-approve

**When to use:** the operator asks to approve multiple pending events at once — "approve everything from the North Tahoe Chamber", "approve the four community meetings I just reviewed".

**READ-ONLY.** This skill *proposes* a bulk approval; it does not execute one. Execution happens via the dashboard's Confirm widget — see [operator-bulk-decline](../operator-bulk-decline/SKILL.md) for the full "why Hermes can't mutate" story. Confirmed approvals also enqueue the first pipeline action (`read-detail`) for each uid, mirroring the dashboard's per-row approve button.

## CLI

```
approve.py --source "north tahoe chamber"
approve.py --before 2026-08-15
approve.py --source "palisades" --limit 10
```

Events held for review (`engine/review_hold.py`: a source mismatch or an
implausible time) are never proposed: approving one accepts what it was held
for, so each needs its own approval with its reason in view (dashboard, or
`autonomy accept <uid>`). They are listed under `skipped_held` — tell the
operator how many and why. The Confirm widget skips them too (`held_uids`).

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
  "preview": [{"event_uid": "…", "title": "…", "date": "…", "source": "…"}, …],
  "skipped_held": {"count": 2, "why": "held for review — …",
                   "events": [{"event_uid": "…", "title": "…", "date": "…",
                               "held_for": ["implausible time"]}, …]}
}
```

## Autonomous mode

Inactive: answers `{"inactive": true, …}` — the agent approves events itself
and TownLoops moderation is the review step (`autonomy why <id>` explains a call).
