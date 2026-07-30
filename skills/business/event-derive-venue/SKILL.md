# event-derive-venue

Workqueue action: **extract-venue**. Runs for events in
`state=approved`.

Reads `venue_raw` (falling back to `venue_name`, then `hosting_group`)
and either matches an existing `venues` row by normalized name, OR
creates a shell venue with just `name` and `derived_from_event_uid`.
Enrichment (website/phone/socials/address) happens later in
`event-enrich-entities`.

## Contract

```
derive.py --event-uid <uid>
```

Exit codes:
- `0` — venue matched or created, `events.derived_venue_uid` set
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
