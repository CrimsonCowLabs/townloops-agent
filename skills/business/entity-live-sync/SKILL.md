# entity-live-sync

Brings local entity changes — mostly enrichment filling blank fields — to
the venue / business / community / performer / creator rows already live
on TownLoops, so research isn't stranded locally. All logic lives in
`engine/entity_sync.py`; event-ship runs the same sync for every entity it
ships, and this skill's daily sweep catches rows changed elsewhere (e.g.
event-reconcile enrichment of an already-shipped venue).

## When an entity is synced

It was created or adopted live by us (`townloops_slug` + `pushed_at`), it
is not a live mirror (`source_kind='live'` — a mirror's local image is an
operator proposal), the server didn't file it under another town
(`extra_json.cross_town` — town-scoped GETs can't see it), it is not flagged, and its `content_hash` differs from
`pushed_hash` (the hash live was last verified to reflect; NULL for an
adopted row). `fill_blanks` refreshes `content_hash` whenever it fills a
field.

## What a sync does

1. **Resync from live** (GET): every non-blank live value is copied into the
   local row. Live wins — an operator edit or a richer live value is kept.
2. **Fill live's blanks**: local values for fields live has empty are sent
   as a full-body PATCH (live row + fills; `engine/live_patch.apply_entity_patch`).
   Never `""` (it would clear), never a value over the API's length limit,
   and a `category_slug` only if it is one of the town's categories (an
   unknown one clears the category).
3. **GET verify**: only when live shows what was sent does `pushed_hash`
   move to `content_hash`.

## Flags for the operator

- **missing** — 404 on the GET or PATCH: the live row was deleted or
  renamed. Never re-created (event-ship won't POST it again either).
- **mismatch** — the PATCH landed but live doesn't show it.

Flagged entities get `extra_json.live_sync = {status, at, detail}` and a
failed `entity-sync` activity (`live-missing` / `live-mismatch`), and no
sync touches them until cleared. Other API errors are transient: nothing is
flagged, `extra_json.sync_error_at` is stamped, and the next run retries it
after the rows that haven't failed lately (so stubborn rows can't starve the
rest). The sweep calls `verify_town()` first.

A listing-page `website` (engine/listing_hosts) is copied neither way — not
pulled into the local row, not sent to fill a live blank — so a website the
listing-website cleanup cleared on one side never comes back.

## Listing-website pass

First in each `run` (engine/listing_website_cleanup.run, see the
`listing-website-cleanup` skill): supervised, the listing-page websites the
operator confirmed are cleared live (PATCH `website: ""` + GET verify), then
locally; autonomous, submitted clears the moderator decided are settled and new
ones submitted as pending changes. Then cleared entities are re-enriched, so an
own site found is synced live by the sweep that follows. A failure here is
logged and never stops the sync.

## Release pass

After the entity sweep (`engine/entity_ship.ship_released`): performers and
creators event-ship held back as bare and unverified that research or the
operator (`performers.py resolve --accept`) has since released are created
through the usual entity ship path when one of their linked events is live —
through `GatedClient` in autonomous mode (a shut gate stops the pass). Each is
a `ship-performer`/`ship-creator` activity naming its live events. A failed
one is stamped `sync_error_at` and retried after the rest; `--kind` narrows
the pass like the sweep (no performer/creator kind → no release pass).

## Series pass

After the entity sweep, under the same lock (`engine/series_sync.py`):

1. **Re-read Series membership** — the live events from 60 days back on are
   listed and each one's `series_slug`/`series_id` is copied onto the local
   rows shipped as it (a curator's or the backfill's later linking — or
   unlinking — reaches local rows here). Never inferred locally.
2. **Fill Series-shared blanks** — for up to `--limit` upcoming occurrences
   this agent shipped that are in a Series: blank description (ticket line
   stripped), categories, tags (minus `free`/`cancelled`/`sold-out`) and
   accessibility notes from the richest sibling, the venue only when every
   sibling with a resolved venue shares it. Full-body PATCH + GET verify;
   outcomes are `series-fill` activities (a failure is `ok=0`). Events this
   agent didn't ship are left alone. See `event-ship/references/recurring-series.md`.

## Commands

```bash
python sync.py run [--limit 25] [--kind venue ...]   # cron: daily 03:30, --limit 15
python sync.py list                                  # pending + flagged (read-only)
python sync.py clear --kind venue --entity-uid UID   # after the operator fixes it
```

`run` takes the workqueue-drain lock (skips quietly while a drain runs) and
is a no-op without `TOWNLOOPS_API_*` env.
