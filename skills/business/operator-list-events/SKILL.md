---
name: operator-list-events
description: List events from the local event-first operator review database by approval state, date cutoff, or discovery source.
---

# operator-list-events

## When to use

Use when the operator asks which locally managed events match a review filter: “list pending events,” “show events from the chamber source,” “what is approved before Aug 15?”, or “which events are declined?”.

Returns one row per event, capped by `--limit` (default 20, max 100). It is read-only and wraps `db_v2.events_by_approval`. Prefer `operator-status` for counts and `operator-review-event` for one-event inspection.

## Scope boundary: local review queue vs. live TownLoops

This skill reads the local event-first review database, not the complete catalog already published on TownLoops. Zero `approved` or `shipped` rows does **not** mean the town has no live events; records created by older pipelines may exist only on the live API.

- Use this skill for review-state questions: pending, approved, declined, shipped, source, or queue state.
- For audience-facing questions such as “what’s on?”, “what events are live?”, or “what’s happening this weekend?”, query the live API with `townloops-api`.
- Default “what’s on” to published live events, not pending local candidates.

## CLI

```bash
python skills/business/operator-list-events/list.py
python skills/business/operator-list-events/list.py --status approved
python skills/business/operator-list-events/list.py --status pending --limit 5
python skills/business/operator-list-events/list.py --status pending --before 2026-08-15
python skills/business/operator-list-events/list.py --source "chamber"
```

- `--status`: `pending`, `approved`, `declined`, or `shipped` (default `pending`)
- `--before`: ISO date or datetime
- `--source`: case-insensitive substring of `discovered_via`
- `--limit`: 1–100 (default 20)

## Output

```json
{
  "status": "pending",
  "count": 3,
  "events": [
    {
      "event_uid": "…",
      "title": "…",
      "date": "…",
      "venue": "…",
      "source": "…",
      "category": "…"
    }
  ]
}
```

A zero-match response is `count: 0` with `events: []`; it is not an error.
