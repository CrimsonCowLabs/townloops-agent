---
name: event-read-detail
description: Read an approved event's own detail page once (Firecrawl + Codex) and store what it states — its accessibility notes, price and ticket/RSVP link, each checked against the page.
---

# event-read-detail

Workqueue action: **read-detail**. First step after operator approval
(`approved → detail-read`, then `extract-venue`). event-find and
event-reconcile only see the seed's listing page; this reads the event's own
`source_url` once. Logic: `engine/event_detail.py`.

## What it stores

- `events.accessibility_notes` — the page's own sentences about access
  (wheelchair/step-free, accessible seating/parking/restrooms, ASL,
  captioning, assisted listening, sensory-friendly sessions, service animals).
  Codex only *picks* sentences; each one is kept only if it is on the page
  (quote check) and names an access feature. "Accessible by trail", "all
  welcome" and a site's accessibility statement never count. Nothing said →
  NULL. Never inferred from the venue type. event-ship sends it as the live
  event's `accessibility_notes` (create, and filling a blank on a live row; a
  live value is never overwritten).
- `events.price` — the page's own words for what it costs to attend (a price
  or range, free, sold out / waitlist, donation / pay-what-you-can), kept only
  if on the page and it states a cost ("Doors at 7:30" or "free parking" never
  do). `events.rsvp_url` — the ticket/RSVP/registration link, kept only if it
  is an http(s) link on the page and not a homepage or the seed's listing
  page. event-ship turns these into the `free` tag or one ticket line atop the
  description (`engine/event_tickets.py`, see event-ship).
- `detail_read_status` / `detail_read_url` / `detail_read_at` — the pass is
  recorded, so a page is read once per `source_url`, not every tick.

A page that itself says the event is cancelled or postponed (a status line,
"this event has been cancelled", or a line naming the event — policy wording
like "refunds if cancelled" never counts) ends the read before the Codex
call: the event is declined with the page's sentence as the note
(`detail-cancelled`) and never ships.

## Budget

One Firecrawl scrape + one Codex call per approved event. Skipped with no fetch
(`no-detail-page`) when `source_url` is missing or is the seed's listing page.
Hourly cap: `workqueue_rate_limits.read-detail` (default 30); the drainer
claims one per heartbeat (6/hour) so the tick stays near Hermes' 120s cron
limit. A read is bounded to ~100s (`engine/event_detail.py` timeouts) to stay
inside the orchestrator's 120s per-skill timeout.

## Failures

A Firecrawl error or empty token bucket, Codex unavailable/throttled, or Codex output with no JSON
object → exit 1, the workqueue retries next tick. On the last attempt the read
is recorded `failed` and the skill exits 0, so the event ships without notes
instead of going stuck. A `failed` read is tried again on the event's next pass
(e.g. a reconcile reopen). A later read that finds nothing never blanks a
stored value.

## References

- `references/accessibility-notes.md` — what counts as access info, what never does.
- `references/ticket-info.md` — what counts as a price, free, a donation or a ticket link.

## Contract

```
read.py --event-uid <uid>
```

Exit codes: 0 read / skipped / gave up, 1 retry, 2 event not found.
