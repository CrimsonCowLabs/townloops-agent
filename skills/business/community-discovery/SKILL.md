---
name: community-discovery
description: Firecrawl-driven sweep for candidate communities in the configured town — including nonprofits and city-government entities, which fold into the communities resource with distinguishing category_slug values (municipal, nonprofit, etc.). Same three-subcommand shape as business-discovery.
---

# community-discovery

Populates `state/townloops.db.discovery_candidates` (with `kind='community'`)
from Firecrawl `/v1/search` and directory scrapes.
Unlike business-discovery, `.gov` hosts are NOT blocked — city government
entities need to be discoverable.

Each candidate carries a `target_category_slug` from its query group so
promotion knows which category to file the row under. TownLoops has no
first-class government or nonprofit endpoints today; both map into
`communities` with `category_slug` = `municipal` or `nonprofit` as the
operator's town setup allows. See `docs/STATUS.md` for the follow-up.

## Commands

Same as business-discovery — `sweep`, `verify`, `promote`, `list`.

```
python skills/business/community-discovery/discover.py sweep [--per-source N]
python skills/business/community-discovery/discover.py verify [--limit N]
python skills/business/community-discovery/discover.py promote [--limit N]
python skills/business/community-discovery/discover.py list [--status ...] [--limit N]
```

## Rhythm

Orchestrator-driven — see business-discovery for the phase flow. Runs
alongside business-discovery in the `discovering` and `verifying` phases.

## Env

Same as business-discovery.

## town-profile.yaml shape (relevant slice)

```yaml
name: "South Lake Tahoe"
region: "California"
discovery:
  community:
    query_groups:
      - category_slug: "municipal"
        terms: ["city government", "town hall", "city council", "chamber of commerce"]
      - category_slug: "nonprofit"
        terms: ["nonprofit organizations", "501c3", "foundation"]
      - category_slug: "community-group"
        terms: ["civic organizations", "community association"]
    seed_directories:
      - "https://tahoenonprofit.org/directory"
```

## Non-goals

- Not for events. Events flow through `event-source-discovery` +
  `event-ingest`.
- Not for individual creators (photographers/artists). Creators are
  currently mined from events; a dedicated creator-discovery skill is
  deferred.
