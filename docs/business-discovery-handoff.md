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
`already-live` and `rejected` candidates are never proposed again.

## Operating it

```bash
python skills/business/business-discovery/discover.py sweep --per-source 25
python skills/business/business-discovery/discover.py verify --limit 20
python skills/business/business-discovery/discover.py list            # verified + new
python skills/business/business-discovery/discover.py list --status already-live
python skills/business/business-discovery/discover.py stats
```

Or the dashboard: `/candidates` lists them (filter by status), and the
operator chat can run `list`. Tune what gets swept in `town-profile.yaml` →
`discovery.business` (`category_query_terms`, `seed_directories`,
`facebook_terms`) and `name_aliases` (extra in-town names the verifier
accepts).

Not scheduled: sweeps spend Firecrawl budget, so run them when you want new
candidates.

## What it does not do

- **It never writes to TownLoops.** A verified candidate is a proposal. Turning
  it into a live business is a separate operator-approved promote step (the
  standard ship path, with a live lookup before any POST) — don't POST a
  candidate by hand.
- It does not research a candidate's details (description, image, socials);
  enrichment does that once a candidate becomes a local business row.

## Leftover CMO work

The CMO's `candidates.db` (17 verified, unpromoted) and `drafts.db` (unshipped
research drafts) exist only in restic snapshot `71b37ab2`; importing them into
this store is a separate one-off job.
