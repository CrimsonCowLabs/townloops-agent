---
name: business-discovery
description: Find local businesses that aren't on TownLoops yet — Firecrawl sweeps, page verification, and dedup against live businesses into a candidate list for the operator.
---

# business-discovery

Finds businesses in this town that TownLoops doesn't list yet. Logic lives in
`engine/business_discovery.py` (candidate store + live dedup) on top of the
`engine/discovery.py` kernel (Firecrawl search/scrape, heuristic page verify,
no LLM). This script is the CLI.

Event mining still creates businesses as a side effect of events; this skill
is for the ones that never host a listed event.

## Flow

1. **sweep** — reads `town-profile.yaml` → `discovery.business`:
   - `category_query_terms`: each becomes `"{term} in {town}, {region}"`,
     `"best {term} {town}"`, `"local {term} {town}"` Firecrawl searches.
   - `seed_directories`: directory pages whose outbound links are candidates
     (site utilities, maps, PDFs and generic section pages are filtered).
   - `facebook_terms` (optional): `site:facebook.com "{town}" {term}`.

   Review sites, maps and wikis are dropped by the kernel's host blocklist.
2. **verify** — scrapes each `new` candidate: the page must name the town (or
   a `name_aliases` entry / the region), show no closure marker, and look
   customer-facing (menu, hours, book, shop, contact…). Address, phone and
   hours are pulled out when present. A candidate with no name (from a
   directory) is named from its page title.
3. **dedup against live** — at sweep *and* verify, every candidate is
   checked against the town's live businesses (fetched fresh each run):
   - its URL is a live business's `website`, or on the same host (social and
     other shared platforms — Facebook, Instagram, Google Sites, … — only
     match on the exact page), or
   - its name is ≥0.85 similar (normalized) to a live business name.

   A match is stored as `already-live` with `live_slug` and the reason, and
   is never proposed again.

## Candidate statuses

| status | meaning |
|---|---|
| `new` | swept, not yet verified |
| `verified` | page checks out — proposed to the operator |
| `rejected` | verifier (or operator) said no; the reason is in `reject_reason` |
| `already-live` | matches a live business (`live_slug`) |

Re-sweeps only revisit `new`/`verified` rows (they may have gone live since);
`rejected` and `already-live` stay decided. A verifier crash (Firecrawl down)
leaves the candidate `new` for the next run.

Candidates are rows in `discovery_candidates` (kind `business`) in
`state/townloops.db`.

## CLI

```bash
python discover.py sweep [--per-source 25]
python discover.py verify [--limit 20]
python discover.py list                       # proposed: verified first, then new
python discover.py list --status already-live # what dedup caught, with live_slug
python discover.py list --status all
python discover.py stats
```

The operator can also browse them on the dashboard's `/candidates` page or
ask in dashboard chat ("which businesses did discovery find?") — answer with
`list`.

## Boundaries

- **Never writes to TownLoops.** Reads the live business list only (after
  `verify_town()`). Turning a verified candidate into a live business is a
  separate, operator-approved promote step — don't POST a candidate yourself.
- Not scheduled: sweeps spend Firecrawl budget, so they run when the operator
  asks (CLI, chat, or the dashboard's sweep action). Both subcommands hold
  the `business-discovery` flock.
- Firecrawl calls share `engine.policy.firecrawl_bucket`.
