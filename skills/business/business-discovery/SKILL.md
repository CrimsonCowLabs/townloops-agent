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
   - `category_query_terms` (and every `query_groups` term, all kinds):
     each becomes two Firecrawl searches (`business_discovery.query_templates`),
     `"{term} {town}, {region} <signal>"` and `"{term} {town} official website"`,
     where the signal is what an own site carries for the kind — `hours contact`
     (business), `about us` (community), `donate` (non-profit). The old
     `"best {term} {town}"` / `"{term} in {town}"` shapes ranked editorial
     roundups above any actual organisation (SLT's harvest: 8 of 25 bad rows
     from two queries), so no shape says "best", "top" or "in {town}".
   - `seed_directories`: directory pages whose outbound links are candidates.
     Filtered (`discovery.directory_link_block_reason`): the seed's own site
     (`www.` and subdomains — the chamber's `business.` directory), every
     other seed directory's site, site utilities (member application, jobs,
     deals, member list, search, calendar, events — `own_site.UTILITY_PATH` +
     `EVENT_PATH`), maps, PDFs, editorial and generic section pages. A seed
     directory is never a candidate of any kind: a search result on one is
     dropped, and verify rejects its pages and a host that redirects to one
     (`tahoesouth.com` → `visitlaketahoe.com`).
   - `facebook_terms` (optional): `site:facebook.com "{town}" {term}`.

   Review sites, maps and wikis are dropped by the kernel's host blocklist.
2. **verify** — scrapes each `new` candidate: the page must name the town (or
   a `name_aliases` entry / the region), show no closure marker, and look
   customer-facing (menu, hours, book, shop, contact…). Address, phone and
   hours are pulled out when present. A candidate with no name (from a
   directory) is named from its page title. The URL must also be the
   organisation's **own** site (`engine/own_site`, the rule performer research
   uses): a homepage, a platform profile, or any page on a host that carries
   its name — a deep page on someone else's host (a travel blog's "24 BEST …"
   post, a city directory) or a site utility page on any host (a chamber's
   `/jobs`, `/member-application`, `/hotdeals` — `own_site.UTILITY_PATH`) is
   rejected with the reason, before any scrape when the candidate already has
   a name. For a community or non-profit, a `.gov` page is the government's
   own site. Don't add blogs to the listing-host list. An article or roundup
   *about* organisations is rejected even on a host that passes — a
   tourism board's "Best Restaurants and Bars in …" (`discovery.roundup_reason`,
   which names the signal): an editorial path (`own_site.ARTICLE_PATH` — blog,
   post, article, visitor/travel guide; the directory link filter drops these
   too); a count or roundup phrase in the URL's slug or the title ("12 Best …",
   "All 196+ … Restaurants", "things to do"; "best"/"top" only beside a plural
   or a place, so "Top Notch Plumbing" is a name); a search-result name cut
   short with "…". Read on the search-result name and on the page's own title,
   whole. A homepage's title is the organisation's name for a phrase ("The
   Best of Tahoe"), not for a count; a title segment the host carries is never
   read.
3. **dedup against live** — at sweep *and* verify, every candidate is
   checked against the town's live businesses (fetched fresh each run):
   - its URL is a live business's `website`, or on the same host (social and
     other shared platforms — Facebook, Instagram, Google Sites, … — only
     match on the exact page), or
   - its name is ≥0.85 similar (normalized) to a live business name.

   The same checks run against the town's live **venues** (a food hall like
   Cascade Kitchens is a venue, never a business or community), for every kind.

   A match is stored as `already-live` with `live_slug` and the reason (a
   venue match reads "matches live venue <slug>: …"), and is never proposed
   or promoted.

## Candidate statuses

| status | meaning |
|---|---|
| `new` | swept, not yet verified |
| `verified` | page checks out — proposed to the operator |
| `rejected` | verifier (or operator) said no; the reason is in `reject_reason` |
| `already-live` | matches a live business, community or venue (`live_slug`) |
| `approved` | the operator approved it — the next `promote` run ships it |
| `promoted` | shipped as a business (`research_entity_uid`, `live_slug`) |

Re-sweeps only revisit `new`/`verified` rows (they may have gone live since);
`rejected` and `already-live` stay decided. A verifier crash (Firecrawl down)
leaves the candidate `new` for the next run. A candidate the **operator**
declined is also remembered by name: a new URL whose name is ≥0.85 similar
lands as `rejected`.

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
python discover.py propose-promote [--uid U ...]  # read-only proposal (all verified by default)
python discover.py propose-decline [--uid U ...]  # read-only proposal
python discover.py promote [--limit 3]            # ships ONLY operator-approved candidates
```

The operator can also browse them on the dashboard's `/candidates` page or
ask in dashboard chat ("which businesses did discovery find?") — answer with
`list`.

## Promoting (operator-gated)

The operator approves a verified candidate with the **Promote** button on
`/candidates`, or by confirming a chat proposal. When they ask in chat to add
/ promote / ship discovered businesses:

1. Run `propose-promote` (with `--uid` for the ones they named; a uid prefix
   works). It changes nothing.
2. Summarize ("3 verified businesses: Brand New Bakery, …. Click Confirm to
   approve them for shipping.") and end the reply with the fenced JSON block
   holding the returned `proposal` and `preview` — the dashboard renders it as
   a Confirm widget (kind `promote_businesses`). Same for declining:
   `propose-decline` → `decline_businesses`.
3. Zero matches → say so and emit no block.

Confirming only marks them `approved`. The `promote` run (cron every 15 min)
then, per candidate: checks live again (→ `already-live`), makes or reuses the
local business, enriches it, and ships it through the standard entity ship
path (live lookup first) → `promoted`. A failed ship or unreadable site stays
`approved` and is retried, behind candidates with fewer attempts
(`promote_attempts`, last failure in `promote_error`). A candidate the
operator declines mid-run is not shipped. Declining one candidate also
declines open candidates of the same business under other URLs. `promote` never touches a candidate the operator hasn't approved, so
running it is always safe — but never claim you approved or shipped anything.

Candidates with `source_signal` `cmo-import:…` came from the CMO's old
discovery run and research drafts (`scripts/import_cmo.py`, one-off). The
CMO's verifier passed listicle and directory pages, so expect the operator
to decline many of those. A `cmo-import:research-draft` candidate carries the
CMO's researched fields (`verify.draft`), which fill the business's blanks on
promote.

## Boundaries

- **sweep / verify / list / propose-* never write to TownLoops.** Reads the
  live business list only (after `verify_town()`). Only `promote` writes, and
  only operator-approved candidates — don't POST a candidate yourself.
- Sweeps are not scheduled: they spend Firecrawl budget, so they run when the
  operator asks (CLI, chat, or the dashboard's sweep action). `promote` is on
  cron (`business-promote`) but spends nothing unless something is approved.
  sweep, verify and promote hold the `business-discovery` flock.
- Firecrawl calls share `engine.policy.firecrawl_bucket`.

## Kinds and autonomous mode

Every subcommand takes `--kind business|community|nonprofit` (default
business; `promote` defaults to all). Community groups and non-profits sweep
`discovery.community` / `discovery.nonprofit` in town-profile.yaml
(`category_query_terms`, `query_groups`, `seed_directories`,
`facebook_terms`), verify against member-facing markers (join, volunteer,
donate, meetings…), and ship as communities — a non-profit with community
`kind` 'nonprofit'.

Live dedup counts every TownLoops status: a pending match is `already-live`
(already submitted), a rejected one is `rejected` with a `townloops` reason.
A moderator rejection (engine/moderation) is remembered **by name**, like an
operator decline: the same organisation under another URL is never proposed
again.

In autonomous mode `harvest` (weekly cron `org-harvest-weekly`) sweeps and
verifies every configured kind and approves verified candidates itself
(`autonomy.judge_candidate` — held while `autonomy.entity_moderation` is off);
the `business-promote` cron then submits them through the gated client, so
they land `pending` in the TownLoops moderation queue. `propose-*` answer
`inactive`.
