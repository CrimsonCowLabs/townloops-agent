---
name: listing-website-cleanup
description: Clear venue, business and community websites that are directory or listing pages (restaurantji, Yelp, maps, ticketing) — live and local, then re-enrich so the entity's own site can fill the blank. Supervised — propose for the operator's Confirm widget; autonomous — pending changes for the TownLoops merge flow.
---

# listing-website-cleanup

Works from `operator-audit-listing-websites`' rows (engine/listing_website_audit.py);
logic in `engine/listing_website_cleanup.py`. A listing page is never an
entity's website (`engine/listing_hosts.is_listing_url`, the one check).

Both sides or neither: clearing only local would be undone, since a live
resync wins. `entity_sync` never copies a listing-page website in either
direction, so a clear holds even while the other side still shows one.

## When to use

The operator asks to fix the listing-page websites the audit found: "clear the
restaurantji websites", "fix the Yelp links". Run the audit first if they want
to see the scope.

## CLI

```bash
python skills/business/listing-website-cleanup/cleanup.py list
python skills/business/listing-website-cleanup/cleanup.py propose-clear [--uid T ...]
python skills/business/listing-website-cleanup/cleanup.py apply [--limit 20]
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
  settles submitted clears and submits new ones. Then re-enriches the cleared
  entities. The daily `entity-live-sync` run does the same pass first, so the
  operator can wait for it or ask for `apply` now.

## Supervised

1. `propose-clear`, then end the reply with the proposal block (SOUL.md
   §Operator chat). Nothing is cleared until the operator clicks Confirm.
2. `apply`, per confirmed entity: live first — a full-body PATCH with
   `website: ""` (the only value that clears), GET-verified — then local. A
   PATCH that doesn't land parks it `failed` with local untouched; it isn't
   retried until the operator confirms it again.

## Autonomous

No confirm step and no PATCH of a published value. A live listing website goes
to the moderator as a pending copy of the live row with `website: null`
(`submissions.kind = 'clear-website'`, `target_slug` the live row), for the
TownLoops merge flow. The hourly moderation poll settles it: only when the
target no longer shows a listing website is the local website cleared
(`cleared`); a rejection, or a merge that left the website, is `not-accepted`
and isn't resubmitted. A listing website that is local only (live has the own
site, or nothing) is cleared locally at once.

## The local clear

- The listing URL is kept as research evidence on the entity
  (`extra_json.listing_pages`).
- Live's own site is taken when live has one. Otherwise `website` is blanked
  and `enrichment_status` set `pending`: re-enrichment searches past listing
  pages for the entity's own site, which then fills the live blank through
  entity-live-sync (a fill, allowed in both modes).
- Live mirrors (`source_kind='live'`) aren't researched locally (`reenrich:
  skipped`).

## Reading the result

Each row's `changed` says what changed: `live`, `local`, `both` or `none` —
always tell the operator which. Statuses: `confirmed` (waiting for apply),
`submitted` (pending in TownLoops), `cleared`, `failed`, `not-accepted`,
`resolved` (no listing website by the time it was applied — nothing changed).
`reenrich`: `queued`, then `enriched` (own site found or other fields filled
— check `website`), `not-found`, or `skipped`.
