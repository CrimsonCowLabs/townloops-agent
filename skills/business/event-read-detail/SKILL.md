---
name: event-read-detail
description: Read an approved event's own detail page once (Firecrawl + Codex) and store what it states — its accessibility notes, price and ticket/RSVP link, age restriction and picture, each checked against the page.
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
- `events.age_restriction` — `adults_only` (21+, 18 and over, adults only) or
  `all_ages`, only from a quoted sentence on the page that states it — and the
  whole page sentence around the quote must say the same ("all ages until 9,
  21+ after" says neither). Never inferred from the venue (a bar) or the kind
  of event. NULL → TownLoops keeps `unrestricted`.
- `events.image_url` / `events.image_alt` — the page's picture: its JSON-LD
  Event `image`, else its og:image, from the same scrape's HTML
  (`engine/images.page_image`). Kept only with alt text the page gives
  (og:image:alt, an ImageObject caption) that describes the picture — never the
  event's title, "event image" or a file name — and never a logo/icon/
  placeholder. It replaces a listing card's or feed's picture.
- `events.detail_description` — only when the listing (or an adapter's feed)
  gave no description, or one under 40 characters: the page's own sentences
  about the event (what happens, who plays, what to expect), each quote-checked
  against the page; dates, prices, ticket and calendar buttons, newsletters and
  other events never count. Kept apart from `description` (the listing's text,
  in the content hash), so a re-read neither loses it nor reads as a change.
  event-ship sends it while `description` is blank (`engine/event_quality`).
- `events.category_slugs` / `events.tags` — only when the row has none: 1–3
  categories picked from the town's vocabulary (`state/categories.json`;
  anything else is dropped) and up to 5 tags. An event with no page of its own
  (`no-detail-page`) gets them from one text-only Codex call over its title,
  venue and description instead — no fetch. Adapter rows carry no
  classification of their own, so this is where most of them get one.
- `detail_read_status` / `detail_read_url` / `detail_read_at` — the pass is
  recorded, so a page is read once per `source_url`, not every tick.

A page that itself says the event is cancelled or postponed (a status line,
"this event has been cancelled", or a line naming the event — policy wording
like "refunds if cancelled" never counts) ends the read before the Codex
call: the event is declined with the page's sentence as the note
(`detail-cancelled`) and never ships. Supervised: the event is flagged
`possibly-cancelled` for the operator instead, and the read goes on.

The page must be this event's (`engine/event_provenance.py`, #45): a heading
names the event and, unless it is a Series page other occurrences share, the
event's section gives its date (posted/updated lines and "other events" lists
don't count). A page that disagrees ends the read before the Codex call
(`held`): the event is held for review in both modes — back in the approval
queue with a `provenance: …` note naming what disagreed, skipped by the
autonomous judge (autonomous records a `provenance-mismatch` hold decision),
listed in every nightly digest until the operator approves it (accepted, never
re-checked) or declines it. Only an approval made after the hold accepts it.

An event whose times are implausible (`engine/time_plausibility.py`, #46) and
not accepted by the operator is held the same way before any fetch, with a
`times: …` note naming each rule it trips (`implausible-time` hold decision).
A hold that can't move the event (another worker or the operator moved it
first) exits 75 — not now — so the drain never advances it unread.

## Budget

One Firecrawl scrape + one Codex call per approved event. Skipped with no fetch
(`no-detail-page`) when `source_url` is missing or is the seed's listing page.
Hourly cap: `workqueue_rate_limits.read-detail` (default 30 claims in any one
hour; `{per_hour: 30, burst: 120}` lifts it to the burst while the read-detail
backlog is deeper than 30). The heartbeat
drain runs detached, so it fetches up to `workqueue_drain.read_detail_per_drain`
pages (default 6) per 10-min drain, started only in the first half of the
drain's `budget_seconds` (default 420) so the rest of the pipeline keeps the
other half; the rest wait for the next drain with no attempt counted. A read is
bounded to ~100s (`engine/event_detail.py` timeouts) to stay inside the
orchestrator's 120s per-skill timeout.

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
read.py backfill [--limit 20] [--dry-run]
```

Exit codes: 0 read / skipped / gave up, 1 retry, 2 event not found.

`backfill` — events already past the detail read, not live, upcoming, that
would be held as thin (`engine/event_quality.missing`): asks only their blanks
of the cached page (no Firecrawl; one Codex call each), or classifies one with
no page from its text. Each event is tried once (`detail-backfill` activity).
Run it after the detail read learns a new field or an adapter is fixed.
