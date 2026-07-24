---
name: business-discovery
description: Firecrawl-driven sweep for candidate businesses in the configured town. Three subcommands (sweep / verify / promote). Auto-promoted rows land in state/townloops.db.businesses and get enqueued for enrichment. Reads seed URLs and query terms from town-profile.yaml.
---

# business-discovery

Populates `state/townloops.db.discovery_candidates` from Firecrawl `/v1/search`
(`{category} in {town}, {region}` templates) and directory scrapes (seed URLs
from `town-profile.yaml → discovery.business.seed_directories`). Adversarial
verify + policy confidence gate before anything is promoted into
`businesses` + `enrichment_queue`.

No LLM, no operator approval — the pipeline is fully autonomous.
Category assignment is keyword-map first (`engine.policy.resolve_category_slug`);
ambiguous cases fall to entity-research in the next phase.

## Commands

```
python skills/business/business-discovery/discover.py sweep [--per-source N]
python skills/business/business-discovery/discover.py verify [--limit N]
python skills/business/business-discovery/discover.py promote [--limit N]
python skills/business/business-discovery/discover.py list [--status ...] [--limit N]
```

- `sweep` — Firecrawl search + directory scrape → insert `status='new'` rows.
- `verify` — for each `status='new'`: re-scrape, apply heuristics, mark
  `verified` or `rejected` with reason.
- `promote` — verified + high-confidence rows only: create `businesses` row,
  enqueue enrichment, mark candidate `promoted`. Low-confidence rows are
  marked `skipped`.
- `list` — read-only inspection.

## Rhythm

Orchestrator-driven. During onboarding, `onboarding-orchestrator tick`
routes: discovering → verifying → researching → enriching → shipping. In
steady state, `discovery-resweep-weekly` cron re-runs sweep once a week to
pick up new listings.

## Env

- `TOWNLOOPS_API_URL`, `TOWNLOOPS_API_KEY`, `TOWNLOOPS_TOWN_SLUG` — TownLoops bearer + slug.
- `FIRECRAWL_API_KEY` OR `FIRECRAWL_API_URL` — Firecrawl auth.
- `HERMES_HOME` — resolves `state/townloops.db` + `state/categories.json`.
- `TOWN_PROFILE_PATH` — overrides `../town-profile.yaml` search.

## town-profile.yaml shape (relevant slice)

```yaml
name: "South Lake Tahoe"
region: "California"
discovery:
  business:
    category_query_terms: ["restaurants", "cafes", "outfitters", "hotels"]
    seed_directories:
      - "https://visitlaketahoe.com/things-to-do/"
    facebook_terms: ["cafe", "boutique"]   # optional; skip section = no FB search
```

## Non-goals

- Not for city government / nonprofits — that's `community-discovery`.
- Not for events — that's `event-source-discovery`.
- Does NOT ship to TownLoops directly; enrichment + entity-research handle that.
