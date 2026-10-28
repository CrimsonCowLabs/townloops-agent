---
name: listing-website-cleanup
description: One-off clear of venue, business and community websites that are directory or listing pages (restaurantji, Yelp, maps, ticketing), then re-enrich so the entity's own site can fill the blank. Supervised — live and local, after the operator's Confirm widget; autonomous — local only, live ones flagged for TownLoops admin.
---

# listing-website-cleanup

Works from `operator-audit-listing-websites`' rows (engine/listing_website_audit.py);
logic in `engine/listing_website_cleanup.py`. A listing page is never an
entity's website (`engine/listing_hosts.is_listing_url`, the one check).

A one-off: it runs only when the operator asks (no cron). `entity_sync` never
copies a listing-page website in either direction, so a local clear holds even
while live still shows one.

## When to use

The operator asks to fix the listing-page websites the audit found: "clear the
restaurantji websites", "fix the Yelp links". Run the audit first if they want
to see the scope.

## CLI

```bash
$PYTHON $HERMES_HOME/skills/business/listing-website-cleanup/cleanup.py list
$PYTHON $HERMES_HOME/skills/business/listing-website-cleanup/cleanup.py propose-clear [--uid T ...]
$PYTHON $HERMES_HOME/skills/business/listing-website-cleanup/cleanup.py apply [--limit 20]
```

- `list` — every clear: `status`, what `changed`, `reenrich` and the
  entity's current `website`. Read-only.
- `propose-clear` — **read-only** proposal (`clear_listing_websites`) for the
  dashboard chat Confirm widget. Reads TownLoops (LIST/GET) and the local DB.
  `--uid` takes targets (`business:<entity_uid>`, or `business#<live id>` for
  a live row with no local row), uid prefixes or names; default is every
  clearable row. Rows whose live side couldn't be read are in `skipped`,
  never proposed. Inactive in autonomous mode.
- `apply` — supervised: clears what the operator **confirmed**; autonomous:
  clears local listing websites and flags live ones. Then re-enriches the
  cleared entities. Runs under the workqueue-drain lock; every subcommand first
  migrates the DB.

## Supervised

1. `propose-clear`, then end the reply with the proposal block (SOUL.md
   §Operator chat — dashboard). Nothing is cleared until the operator clicks Confirm.
2. `apply`, per confirmed entity: live first — a full-body PATCH with
   `website: ""` (the only value that clears), GET-verified — then local. A
   PATCH that doesn't land parks it `failed` with local untouched; it isn't
   retried until the operator confirms it again.

## Autonomous

No confirm step (`propose-clear` and the widget answer inactive) and no PATCH
of a published value. `apply` clears every listing website the audit finds
locally. Where **live** holds one it is `flagged` — "live still holds a
listing page; fix in TownLoops admin" — and listed in the nightly digest's
"Listing websites live" section until a later `apply` sees live fixed
(`resolved`). Autonomous live clears await a TownLoops entity
change-submission endpoint; until then they are an admin fix. A target is
handled once; re-running `apply` resolves flags and picks up rows past `--limit`.

## The local clear

- The listing URL is kept as research evidence on the entity
  (`extra_json.listing_pages`).
- Live's own site is taken when live has one. Otherwise `website` is blanked
  and `enrichment_status` set `pending`: re-enrichment searches past listing
  pages for the entity's own site, which fills the live blank through
  entity-live-sync once live's website is blank (supervised; autonomous waits
  for the admin fix).
- Live mirrors (`source_kind='live'`) and live rows with no local row aren't
  researched (`reenrich: skipped`): enrichment never fills a mirror, so their
  own site is set in TownLoops admin — the row's note says so.

## Reading the result

Each row's `changed` says what changed: `live`, `local`, `both` or `none` —
always tell the operator which. Statuses: `confirmed` (waiting for apply),
`cleared`, `failed` (live PATCH didn't land; re-confirm), `flagged`
(autonomous: live still holds it), `resolved` (no listing website any more —
nothing changed).
`reenrich`: `queued`, then `enriched` (own site found or other fields filled
— check `website`), `not-found`, or `skipped`.
