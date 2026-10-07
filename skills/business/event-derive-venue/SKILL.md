# event-derive-venue

Workqueue action: **extract-venue**. Runs for events in
`state=approved`.

Reads `venue_raw` (falling back to `venue_name`, then `hosting_group`)
and binds the event to a venue, in this order:

1. A local `venues` row with the same normalized name that is already on
   TownLoops (`townloops_slug` set).
2. A venue already **live on TownLoops** — exact normalized name, or a close
   spelling of exactly one live name. It is mirrored locally
   (`source_kind='live'`, live id in `extra_json`) so event-ship never POSTs
   it again. Same-name live duplicates resolve to the richest row (most
   populated fields / image). The live venue list is fetched once and cached
   for 10 minutes in `state/cache/live-venues.json` (one drain run).
3. An unshipped local row with the same normalized name.
4. Otherwise a new shell venue (`name` + `derived_from_event_uid`).
   Enrichment (website/phone/socials/address) happens later in
   `event-enrich-entities`.

Only live **venues** count — a live business with the same name is not a
venue and cannot bind an event (a business row ≠ a venue row).

A guess — the name only overlaps a live venue by tokens ("Gunbarrel" vs
"Gunbarrel Tavern", "Cuppa" vs "Cuppa Tahoe"), or is close to several
different live venues — is
**parked for the operator** (`stuck-approved`) instead of creating a shell
that would ship as a duplicate. The candidates are in the queue row's
`last_error` and the activity log. To resolve: set the event's `venue_raw`
to the exact live venue name (links it), or to a distinct name (creates a new
venue), then unstick the event.

## Contract

```
derive.py --event-uid <uid>
```

Exit codes:
- `0` — venue matched or created, `events.derived_venue_uid` set
- `1` — ambiguous live match (event parked for the operator), or the live
  venue lookup failed (retried; never falls back to creating a shell)
- `2` — event has no venue signal at all (venue_raw + venue_name + hosting_group all NULL). The orchestrator will surface this as a failure; operator can add a venue manually or decline the event.

## Idempotency

`entity_uid = sha1("venue::" + normalize(name) + "::" + event_uid)`.
Re-running against the same event produces the same uid, but the
`INSERT OR IGNORE` in `venues` means a second run against a
different event pointing at the same normalized venue name will still
share the pre-existing row (the normalize lookup runs first).

## Not doing (yet)

- Address extraction — belongs in enrich-entities.
- LLM disambiguation ("Community Center" at Kings Beach vs Incline
  Village). We rely on normalize_name being conservative and trust
  event-find to include enough context in `venue_raw` (usually town
  is embedded in the source seed's URL and gets folded into the LLM's
  extraction).
