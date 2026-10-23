# event-ship

Workqueue action: **ship**. Runs for events in
`state=entities-enriched`.

POSTs derived entities FIRST, then the event, to TownLoops. Ordering
matters — the event references its venue by live `venue_id`, so the venue
must exist first. Per plan adversarial pass #19: venue → host → performers →
creators → event. A bare performer/creator research hasn't verified is held
(`ship-performer` activity "held (awaiting research: …)") and the event ships
without it; `entity-live-sync` creates it once research releases it.

## Contract

```
ship.py --event-uid <uid>
```

Exit codes:
- `0` — event POSTed, or found live and filled in; `events.pushed_at` + `events.townloops_slug` set.
- `1` — TownLoops API returned a non-recoverable error (401/403/404 on town endpoint, malformed payload). Orchestrator retries up to 3 times then marks stuck.
- `2` — event not found in local DB.

## Idempotency

Each POST uses `Idempotency-Key: hermes-{town_slug}:ship:{entity_kind}:{entity_uid}`.
Replaying the same request (same key) is safe — TownLoops returns 200 with
the existing record. A *different* request that hits a taken slug gets a 409,
which is handled below.

## Look up live before creating

Nothing is POSTed until a lookup says it isn't live yet
(`engine/live_lookup.py`). A lookup error is rc 1 (retry), never a blind create.

- **Entities**: GET by slug (the row's `townloops_slug`, else the proposed
  `hermes-<town>-…` slug). Found → adopted: `townloops_slug` and
  `extra_json.live_id` recorded, nothing POSTed. Created → the live id
  from the create response is recorded the same way. Either way (and for a
  row shipped earlier whose content changed since, e.g. by enrichment) the
  entity is then **synced** (`engine/entity_sync.py`, see the
  `entity-live-sync` skill): live values copied locally, live blanks filled
  by a full-body PATCH, GET-verified before `pushed_hash` moves. A row we
  shipped before that now 404s is flagged for the operator
  (`extra_json.live_sync`), never re-created.
- **Events**: GET by slug; then, when the event will join a TownLoops Series
  (see "Series" below), that Series' occurrences on the same town-local date
  (`?series=<slug>&from=<day start>&to=<day end>`) — one is this event (a
  second source's copy of one evening, whatever its source_url or start), two
  or more are a duplicate for the operator; then — the list endpoint has no
  `source_url` filter — list the events starting within 12h and match `source_url`
  (trailing `/` ignored; with several, the one at the same start wins).
  Found → **filled, not overridden**: blank live fields get the local value,
  local categories/tags are added to the live sets, a venue is bound only if
  the live event has none; sent as a full-body PATCH + GET verify
  (`engine.live_patch.apply_event_patch`), nothing sent when there's nothing
  to add. Several live rows for one event → a failed `find-live-event`
  activity listing their slugs, the event parked `stuck-` for the operator.
- **409 `slug_taken` is not success**: ship looks the row up again and
  continues as "found"; if it still isn't there, rc 1.

## Series (ADR 0007)

A TownLoops **Series** is the evergreen hub for Events that recur under one
name. The agent never creates, renames or archives one (no API write surface;
the client refuses `series` writes). An Event joins a Series only when
TownLoops auto-links its title — same `series_key`, an *active* Series in the
town — at create. So ship decides the title (`engine/series.py:decide_title`,
recorded as a `series-title` activity; the local title never changes):

- a title variant ("Trivia Thursday – Week 3") or a title from the same
  source/source branch as the Series' occurrences ships under the **Series
  title** — only when the derived venue is the Series' one resolved venue;
- a performer-only title (it is a derived performer's/creator's name) whose
  key matches a Series at another venue, or that recurs at other venues,
  ships as **"<Performer> at <Venue>"** — never suffixed twice;
- otherwise the source title. An archived Series is never a target.

The Series snapshot is `state/series.json` (refreshed by the daily API check
and lazily here, 10-min TTL). After shipping, ship GETs the event, records
the `series_slug`/`series_id` TownLoops reports on the local row, and fills
the event's blank **Series-shared** fields — description (ticket line
stripped), categories, tags (minus `free`/`cancelled`/`sold-out`),
accessibility notes, and the venue only when every sibling with a resolved
venue shares it — from the richest sibling (full-body PATCH + GET verify; a
failure is a failed `series-fill` activity, the event stays shipped). Date,
times, slug, title, price/ticket line and cancellation marks never copy.
TownLoops has no event image field, so images aren't shared.

## Accessibility notes

`events.accessibility_notes` (quote-checked by `event-read-detail`) goes out as
`accessibility_notes` in the create body. A live event that has its own value
keeps it: an already-live row (lookup or reconcile `changed` PATCH) gets ours
only when its field is blank, and the full-body PATCH resends the live value
otherwise. A NULL is never sent. A create that drops the notes is repaired
like dropped tags (full-body PATCH + GET verify, else flagged).

## Ticket / price info

TownLoops has no ticket fields, so `events.price` / `events.rsvp_url` (from
`event-read-detail`) ship as (`engine/event_tickets.py`):

- **Tags** — a stated-free event (free, $0) gets `free`; a stated price, sold
  out notice or suggested donation drops `free` from the create body. No price
  stated → tags as extracted.
- **Description** — a paid, donation or RSVP event gets one line on top:
  `Tickets: $25–$40 — <link>`, `Tickets: $30 (sold out; waitlist) — <link>`,
  `Tickets: by donation, $10 suggested`, `RSVP: <link>`. The body is cut to fit
  the 5000-char limit, never the line. On an already-live row (lookup fill or
  reconcile PATCH) the line goes on top of the live description, replacing an
  earlier ticket line, so re-ships are no-ops; a free event adds `free` to the
  live tags. Built at ship time: the local description and content hash
  never change.

## Venue by id

The event payload carries `venue_id` — the derived venue's recorded live id,
else its slug resolved with a (town-scoped) GET. The API has no `venue_slug`
field, and a `venue_name` alone that matches several in-town venues fails
server-side ("Multiple rows were found"); with a `venue_id` the server binds
by id and keeps `venue_name` only as the curator's hint if the id doesn't
resolve. A venue the server filed under another (ghost) town, e.g. Reno, gets
no live id recorded — `venue_id` resolves in-town only — so its events bind by
name. If a name-only event's venue is ambiguous live, ship records why and
exits 1 — derive/link the venue.

## Times are UTC only

`start_at` / `end_at` come from the `date_utc` / `end_at_utc` columns, which
`engine.event_extract.upsert_event` fills from the extracted local times in the
town timezone (`$AGENT_TZ`). A row queued before those columns were set is
converted the same way at ship time. An event with no UTC start is **not
shipped**: ship records a failed `ship-event` activity and exits 1, so the
workqueue retries and then marks it `stuck-` for the operator. A naive local
date is never sent; an end time with no UTC form is left off.

An implausible time (#46: a pre-dawn town-local start, an end before its
start, an over-long span — the rules and thresholds live only in
`engine/time_plausibility.py`) the operator hasn't accepted is **not
shipped**: the event goes back to the approval queue with a `times: …` note
naming each rule, and ship exits 0 with nothing sent. If the hold can't move
it (another worker or the operator moved it first), ship exits 75 — not now,
retried — so it is never advanced to shipped unsent. An approval after the
hold ships it with those times.

## Categories and tags

The event goes out with `category_slugs` (all of its categories) and `tags`;
TownLoops' create schema has no singular `category_slug`. Categories are the
row's `category_slugs` (or legacy `category_slug`) mapped onto the town's live
slugs in `state/categories.json` (`engine/categories.py`) — old extraction
vocabulary like `music`/`food` becomes `live-music`/`food-drink`, and anything
that maps to no live slug is dropped. Tags are sent as slugs, which is what the
API stores and returns.

A create path has been seen to drop `tags` (CMO, bulk create). So when the
created row lacks a category or tag set that was sent, ship follows with a
full-body PATCH (`engine.live_patch.apply_event_patch`) and GET-verifies it.
If the sets still don't stick, the event's live slug is recorded, a failed
`verify-live-event` activity is logged, the event is parked `stuck-` for the
operator, and ship exits 1. A requeued event already live goes through the
lookup above, whose fill-in adds any missing categories/tags. With no
`state/categories.json` snapshot, events ship without categories and ship
warns on stderr — run `town-init setup`.

## When TownLoops isn't ready yet

If `TOWNLOOPS_API_KEY` env is missing or empty, this skill logs the
event as "shipping disabled" and exits 0. The event state advances to
`shipped` in the local DB even though nothing was POSTed. This lets
the operator visualize the whole pipeline running end-to-end during
town onboarding, before TownLoops.com even has the town record.

When the town is finally registered on TownLoops, an operator-triggered
`resync` will POST the accumulated shipped-locally events.

## Reconcile-staged rows

- An event whose `extra_json.reconcile.kind == "changed"` (staged by
  `event-reconcile`) is a source-side change to an existing live event: ship
  **PATCHes** `events/<live_slug>` (full body, GET-verified) with the changed
  `start_at`/`end_at` and/or the new venue's `venue_id` (see "Venue by id"),
  instead of POSTing a new event. On a date change with no usable
  source end time, the live end is shifted by the same amount as the start;
  an end before the start is never sent (rows staged without the live times
  read them with a GET first). A venue change whose new venue has no live id
  or slug yet fails with rc 1 for a workqueue retry rather than PATCHing only
  the name. Once a date-change PATCH lands, the older local row for the old
  date is marked `status='superseded'` and loses its `townloops_slug`.
- Linked entities with `source_kind='live'` are local mirrors of TownLoops rows;
  ship returns their `townloops_slug` without POSTing.

## References

- `references/recurring-series.md` — Series-shared enrichment keyed on `series_slug`, without touching per-occurrence fields; idempotent title rules.

## Autonomous mode

The client is an `engine/autonomy.GatedClient`:

- **Gate first**: paused, circuit open or `autonomy.max_pending` reached →
  exit 75; the drainer releases the claim without spending an attempt (it
  doesn't even claim `ship` while the gate is shut).
- **Fail closed**: every create must come back `pending`. A `published`
  answer means the key can publish — the circuit opens, the operator is
  alerted, and nothing else is submitted.
- **Lookups count pending and rejected records**: a live match TownLoops
  rejected means "don't submit" (declined locally, `rejected-on-townloops`); a
  pending one is adopted and its blanks filled.
- **New entities** are submitted pending (the TownLoops entity gate);
  approving the event publishes the pending venue and hosts it links to.
  With `autonomy.entity_moderation: false` the event ships with its venue's
  name only, for the moderator's curate-venue flow, and the held entity is
  logged as a `ship-<kind>` activity reading `held (<reason>)`, not a
  failure. Existing live entities are still adopted and filled.
- **Changes to a published event**: every change event-reconcile staged
  lands somewhere. One to a value the live row already has (date, venue, any
  other field) is submitted as a pending event carrying the same
  `source_url`, for the TownLoops merge flow — never PATCHed. One that only
  fills a blank live field (e.g. a venue on a venue-less event), plus the
  accessibility-notes / ticket-line / `free` fills, is PATCHed live and
  GET-verified. A venue with no live id goes in the submission by name. Each
  outcome is a `change` decision (`submit`, `patch`, or `skip` when the live
  row already shows it or the source has no value to send). What the submission carried is kept on the event
  (`extra_json.change_submission`), and the moderation poll reads it as
  merged (re-pointing the local row) once the target shows any of it.
- Blank fields are first filled from the best other copy of the event
  (`event_dedup.best_of_copies`: primary source before syndicator, then seed
  trust).

Every create is recorded in `submissions` (kind, seed, Series) for the
moderation poll.
