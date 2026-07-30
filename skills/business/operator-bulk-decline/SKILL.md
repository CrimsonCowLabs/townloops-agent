# operator-bulk-decline

**When to use:** the operator asks to decline multiple events at once — "decline all pending events from Sierra Sun", "reject every event before today".

**READ-ONLY.** This skill *proposes* a bulk decline; it does not execute one. Under the widget-based confirmation flow, execution happens only when the operator clicks Confirm on the widget the chat panel renders from the agent's reply. Hermes physically cannot mutate — the `--confirmed` code path was removed after Hermes was observed bypassing an earlier conversational contract.

## CLI

```
decline.py --status pending --before 2026-08-01
decline.py --source "sierra sun"
decline.py --status approved --before 2026-08-15
```

Filters (at least one of `--before` / `--source` REQUIRED — bare form refused as a footgun):

- `--status`  ∈  {pending, approved}   (default: pending)
- `--before`  ISO date/datetime
- `--source`  substring match on `discovered_via` (case-insensitive)
- `--limit`   1..500 (default 500)

## Output shape

```json
{
  "proposal": {
    "kind": "decline_events",
    "filters": {"status": "pending", "before": "2026-08-01", "source": null},
    "count": 5,
    "uids": ["…", "…", …]
  },
  "preview": [
    {"event_uid": "…", "title": "…", "date": "…", "source": "…"},
    …
  ]
}
```

Hermes wraps this into a natural-language reply for the operator AND ends its message with a fenced JSON block containing `proposal` — the chat worker extracts the block into `chat_messages.tool_call_json` so the panel renders a Confirm/Cancel widget.
