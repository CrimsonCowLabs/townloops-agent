---
name: event-source-discovery
description: Build the town's sources.yaml — record candidate event feeds, probe them for iCal/JSON-LD/RSS, get user approval, compile to sources.yaml. Use when the town has no sources yet or when the user asks to add a new event source.
---

# event-source-discovery

The agent drives web searches for candidate event feeds; this CLI persists them, probes each for a machine-readable format, waits for approval, and compiles the approved set into `state/sources.yaml`. `event-ingest` consumes that file.

## When to use

- Fresh deployment — no `state/sources.yaml` yet.
- User says "add a new event source", "look for more calendars", "find events from `<venue>`".
- After a source goes stale (0 events for 2+ weeks), remove and hunt for replacements.

## The agent's role

1. **Search.** Use the web/browser tools to run queries like:
   - `"<town> chamber of commerce events"`
   - `"<town> tourism calendar"`
   - `"<town> library events"`
   - `site:<town>.gov calendar`
   - `"<venue> events calendar"`
2. **Record hits.** For each promising URL, run:
   ```
   discover.py add <url> --title "Truckee Chamber events" --method "search:chamber"
   ```
3. **Probe.** Batch-probe all `proposed` candidates:
   ```
   discover.py probe --limit 20
   ```
   Successful probes flip candidates to `needs_review`.
4. **Show the user.** Run `discover.py list --status needs_review`; present each candidate + detected feed URL + parser to the user in Telegram. Ask approve/reject.
5. **Record decisions.**
   ```
   discover.py approve <url>          # uses detected feed + parser
   discover.py approve <url> --feed-url ... --parser jsonld  # override
   discover.py reject <url> --reason "duplicates chamber calendar"
   ```
6. **Compile.**
   ```
   discover.py compile
   ```
   Regenerates `state/sources.yaml`. Idempotent — safe to run any time.

## Environment

- `TOWNLOOPS_TOWN_SLUG` (used only in the compiled sources.yaml header)
- `SOURCES_YAML_PATH` (default `$HERMES_HOME/state/sources.yaml`)
- `DISCOVERY_DB_PATH` (default `$HERMES_HOME/state/discovery.db`)

No TownLoops API touch. Purely local.

## The probe

For each candidate URL, tries in order:
- Common iCal suffixes (`?ical=1`, `/calendar.ics`, `/events.ics`, `/ical`, `/feed.ics`).
- `<link rel="alternate" type="text/calendar">` in the HTML head.
- Feed suffixes for RSS/Atom (`/feed`, `/rss`, `/rss.xml`, `/atom.xml`) and `<link rel="alternate" type="application/{rss,atom}+xml">`.
- Counting `application/ld+json` blocks with `@type: Event`.
- Heuristic HTML-calendar patterns as a last resort.

Anything positive → candidate flipped to `needs_review` with the detected feed URL stashed.

## Compiled sources.yaml shape

```yaml
generated_at: "..."
town_slug: truckee
sources:
  - name: "Truckee Chamber events"
    type: ical
    url: "https://truckee.com/events?ical=1"
    notes: "search:chamber"
  - name: "Alibi Ale Works"
    type: jsonld
    url: "https://alibialeworks.com/events"
```

The `type` matches `event-ingest`'s parser registry: `ical | jsonld | rss | html | firecrawl`.

## MUST ask the user

Approving a source is authoritative — mistakes propagate for weeks. Always present the candidate list to the user and confirm each approval individually. Only `reject` (with reason) can be done autonomously if a probe returns 4xx/5xx or duplicates an already-approved source.
