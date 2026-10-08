# event-ship

Workqueue action: **ship**. Runs for events in
`state=entities-enriched`.

POSTs derived entities FIRST, then the event, to TownLoops. Ordering
matters — the event references its venue by live `venue_id`, so the venue
must exist first. Per plan adversarial pass #19: venue → host → performers →
creators → event.

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
- **Events**: GET by slug, then — the list endpoint has no `source_url`
  filter — list the events starting within 12h and match `source_url`
  (trailing `/` ignored; with several, the one at the same start wins).
  Found → **filled, not overridden**: blank live fields get the local value,
  local categories/tags are added to the live sets, a venue is bound only if
  the live event has none; sent as a full-body PATCH + GET verify
  (`engine.live_patch.apply_event_patch`), nothing sent when there's nothing
  to add. Several live rows for one event → a failed `find-live-event`
  activity listing their slugs, the event parked `stuck-` for the operator.
- **409 `slug_taken` is not success**: ship looks the row up again and
  continues as "found"; if it still isn't there, rc 1.

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

- `references/recurring-series.md` — copying shared enrichment across a recurring series without touching per-occurrence fields; idempotent title prefixes.
