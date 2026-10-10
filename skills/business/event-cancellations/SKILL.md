---
name: event-cancellations
description: Live events that vanished from all their sources or that their listing marks cancelled — list them, propose confirm/dismiss for the operator, and mark operator-confirmed ones cancelled on TownLoops.
---

# event-cancellations

An event that disappears from its source before it happens may have been
cancelled. Detection runs inside `event-find` (engine/event_cancel.py); this
skill is the operator's side of it. Nothing is cancelled automatically.

## How an event gets flagged

- After each **successful** event-find run of a seed (it scraped and
  extracted at least one event), every upcoming row from that seed dated no
  later than the furthest event the run listed is either seen
  (`missed_finds` reset) or missed (`missed_finds` + 1). Events past what the
  page shows, failed scrapes and empty extractions count nothing.
- An upcoming event that is **on TownLoops** is flagged
  `possibly-cancelled` when every seed-found copy of it (all rows sharing its
  `canonical_event_uid` — see engine/event_dedup.py) has missed
  `cancellation_miss_threshold` finds in a row (town-profile.yaml, default 4
  — two days of twice-daily finds).
- If any copy is listed again, the flag clears by itself.

A flag can also mean the event moved to another date or was renamed past
recognition — check the source before confirming.

## Still listed, but marked cancelled

A seed may keep listing an event it marks cancelled or postponed
("CANCELLED: Jazz Night", "this event has been postponed"). event-find keeps
such a statement only if the listing page carries it (quote check), takes
the marker off the title so it still matches its row, and then:

- **live** — flags it `possibly-cancelled` (`cancel_note`: `listed as
  cancelled by <seed>: "<quote>"`), the same as a vanished event. The flag
  holds while the listing keeps saying so and clears when it stops.
- **not shipped yet** — declines it with the quote as the note. read-detail
  does the same when the event's own page says so.

## CLI

```bash
python skills/business/event-cancellations/cancellations.py list
python skills/business/event-cancellations/cancellations.py propose-confirm [--uid U ...]
python skills/business/event-cancellations/cancellations.py propose-dismiss [--uid U ...]
python skills/business/event-cancellations/cancellations.py apply [--limit N]   # cron
```

- `list` — flagged events (`possibly-cancelled`, or `apply-failed`), soonest
  first, with the live slug and why they were flagged. Read-only.
- `propose-confirm` / `propose-dismiss` — **read-only** proposals
  (`confirm_cancellations` / `dismiss_cancellations`) for the dashboard chat
  Confirm widget. `--uid` takes uids or prefixes; default is all flagged.
- `apply` — for every event the operator **confirmed**: GET the live event,
  prefix its title with `Cancelled: ` and add the `cancelled` tag as a
  full-body PATCH (engine/live_patch), then GET-verify. An event already gone
  from TownLoops counts as cancelled. A PATCH that doesn't land parks the
  event as `apply-failed` (shown by `list`) — it's not retried until the
  operator confirms again. With nothing confirmed it exits without touching
  TownLoops (cron every 30 min).

TownLoops has no cancelled status (PATCH can't set `status`; DELETE would
remove the page people may have bookmarked), so cancellation is shown in
the title and tag.

## Statuses (`events.cancel_status`)

| status | meaning |
|---|---|
| `possibly-cancelled` | flagged; waiting on the operator |
| `confirmed` | operator says cancelled; the next `apply` marks it live |
| `cancelled` | marked cancelled on TownLoops (or already gone) |
| `apply-failed` | the live marking didn't land; `cancel_note` says why |
| `dismissed` | operator says it's still on; never flagged again |

## Autonomous mode

No confirm step and no live title PATCH. `apply` (same cron) re-reads up to
three flagged events' own pages (each at most daily); only when the page
**explicitly** says cancelled or postponed (when the event's `source_url` is
the seed's listing, a line that names the event) is a pending "Cancelled: <title>"
copy (with the `cancelled` tag and the same `source_url`) submitted for the
TownLoops merge flow (`cancel_status` 'submitted'). A flag with no such
statement is never acted on — it is listed in the nightly digest. `propose-*`
answer `inactive`.
