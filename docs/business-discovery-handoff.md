# Business discovery — per-town operator notes

Web-wide discovery of businesses that *should* be on TownLoops but aren't yet
now runs **in this repo**, as the `business-discovery` skill. It used to be a
CMO-level function (`~/.hermes/skills/business/townloops-business-discovery/`);
that skill was archived and its scripts deleted, so the town agent owns it.

## Big picture

```
town-profile.yaml → discovery.business
        │
        ▼
business-discovery sweep ──► discovery_candidates (state/townloops.db)
   (Firecrawl search,             status: new
    seed directories,                 │
    optional Facebook)                ▼
        │                  business-discovery verify
        │                    (scrape page: in town? open? customer-facing?)
        │                         │
        ▼                         ▼
  GET live businesses ──► dedup ──► verified │ rejected │ already-live
  (TownLoops, read-only)
```

- Code: `skills/business/business-discovery/discover.py` (CLI) →
  `engine/business_discovery.py` (store + live dedup) → `engine/discovery.py`
  (Firecrawl kernel, no LLM).
- Event mining still creates businesses as a side effect of events. Discovery
  covers the ones that never host a listed event.

## Dedup against live

Each run fetches the town's live businesses and marks a candidate
`already-live` (with `live_slug` and the reason) when:

- its URL is a live business's `website`, or a page under it on the same host
  (a chain's per-location website like `laketahoealeworx.com/stateline` only
  covers that location's pages; Facebook/Instagram/Google Sites and other
  shared platforms only match on the exact page), or
- its name is ≥0.85 similar to a live business's name (normalized, as in the
  CMO's `already_in_townloops`).

The check runs at sweep and again at verify (live may have grown, and a
directory candidate only gets a name from its page title at verify).
`already-live` and `rejected` candidates are never proposed again. A
candidate the operator declined is also remembered by name: a later
candidate under another URL whose name is ≥0.85 similar lands as `rejected`
("operator declined … earlier"). A verifier rejection isn't remembered that
way — the same business may verify fine from a better page.

## Operating it

```bash
python skills/business/business-discovery/discover.py sweep --per-source 25
python skills/business/business-discovery/discover.py verify --limit 20
python skills/business/business-discovery/discover.py list            # verified + new
python skills/business/business-discovery/discover.py list --status already-live
python skills/business/business-discovery/discover.py stats
python skills/business/business-discovery/discover.py propose-promote [--uid U]  # read-only
python skills/business/business-discovery/discover.py propose-decline [--uid U]  # read-only
python skills/business/business-discovery/discover.py promote --limit 3          # approved only
```

Or the dashboard: `/candidates` lists them (filter by status), and the
operator chat can run `list`. Tune what gets swept in `town-profile.yaml` →
`discovery.business` (`category_query_terms`, `seed_directories`,
`facebook_terms`) and `name_aliases` (extra in-town names the verifier
accepts).

Sweeps are not scheduled: they spend Firecrawl budget, so run them when you
want new candidates.

## Promoting a candidate (operator-gated)

Nothing a sweep finds reaches TownLoops until the operator approves it
(`engine/business_promote.py`):

1. **Approve** — the **Promote** button on a verified business row in
   `/candidates`, or in dashboard chat: the agent runs `propose-promote` and
   the operator clicks **Confirm** on the widget (`promote_businesses`). Either
   flips the candidate `verified` → `approved`. Neither the agent nor any CLI
   flag can approve.
2. **Promote run** — `discover.py promote` (cron `business-promote`, every
   15 min via the detached `scripts/business_promote.py`; ≤3 per run; no API
   call when nothing is approved) takes only `approved` rows:
   - checks live again (website / ≥0.85 name) → `already-live` if it's there now;
   - makes the local business (or reuses an unshipped local business of the
     same name) from the candidate page (`website`, or `facebook`/`instagram`/
     `tiktok` for a social page) and the verified address/phone;
   - enriches it (`engine/entity_enrich`, fill-blanks);
   - ships it through `engine/entity_ship.ship_entity` — the same path
     event-ship uses: live lookup by slug, create, 409 → re-lookup, then
     `entity_sync` fill + GET verify.
   Shipped → `promoted` (`research_entity_uid`, `live_slug`). A failed ship or
   an enrichment that couldn't read the site leaves it `approved` for the next
   run.
3. **Decline** — the **Reject** button, or chat `propose-decline` + Confirm
   (`decline_businesses`). An approved candidate can still be declined before
   the run picks it up.

## What it does not do

- **Sweep/verify never write to TownLoops.** A verified candidate is a
  proposal; only the promote run ships, and only what the operator approved —
  don't POST a candidate by hand.

## Leftover CMO work

The CMO's `candidates.db` (17 verified, unpromoted) and `drafts.db` (unshipped
research drafts) exist only in restic snapshot `71b37ab2`; importing them into
this store is a separate one-off job (they should land as `verified`, so they
go through the same approval).
