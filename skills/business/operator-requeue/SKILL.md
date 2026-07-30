# operator-requeue

**When to use:** the operator asks to retry a stuck workqueue item — "requeue that failed shipping", "the extract-venue for event X is stuck, retry it".

Resets a single `event_workqueue` row so the next heartbeat retries the action: `attempts=0`, `last_error=NULL`, `claimed_by/claimed_at` cleared, `resolved_at` stays NULL. Wraps `db_v2.workqueue_requeue` (inlined here since dashboard/ isn't mounted agent-side).

Not confirmation-gated in v1 — requeue is idempotent and the risk of an accidental requeue is low (worst case: one wasted skill run). If the operator asks to requeue "everything stuck", the agent can loop `operator-requeue` per queue_id after showing them the list via `operator-status` or a follow-up call.

## CLI

```
requeue.py --queue-id N
```

## Output shape

```json
{"queue_id": 42,
 "requeued": true,
 "was_resolved": false,
 "event_uid": "…",
 "next_action": "extract-venue"}
```

`requeued: false` when the queue_id doesn't exist or was already resolved.
