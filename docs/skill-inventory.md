# Skill inventory

One-page reference for what each skill does + its most common commands.

| Skill | CLI | When |
|---|---|---|
| `town-init` | `init.py setup` / `init.py status` | Once at deploy; anytime for a health check. |
| `event-source-discovery` | `discover.py add / probe / list / approve / reject / compile` | New town OR user wants a new source. |
| `event-ingest` | `ingest.py ingest / ship / resync / kinds / list-sources / validate` | Cron 06:00 & 18:00 + ad-hoc. |
| `venue-enrichment` | `enrich.py sweep / ship / depth` | Cron 09:00 + ad-hoc. |
| `business-enrichment` | `enrich.py sweep / ship / depth` | Cron 09:00 + ad-hoc. |
| `performer-enrichment` | `enrich.py sweep / ship / depth` | Cron 09:00 + ad-hoc. |
| `newsletter-weekly` | `newsletter.py compose / push / show` | Cron Sun 08:00 compose; operator triggers push. |
| `sponsor-management` | `sponsors.py add-sponsor / list-sponsors / assign / audit` | Ad-hoc + cron Mon 10:00 audit. Gated by `SPONSORS_ENABLED`. |

## Data flow

```
event-source-discovery  →  state/sources.yaml
        ↓
event-ingest            →  state/townloops.db (events + mined venues/businesses/performers)
        ↓ (queues per-kind)
venue-/business-/performer-enrichment
        ↓ (PATCH remote)
event-ingest ship       →  TownLoops API (deltas)
        ↓
newsletter-weekly compose
        ↓ (operator approves)
newsletter-weekly push  →  Listmonk
```

Sponsors ride alongside: `sponsor-management assign` sets `ad_spots`, `newsletter-weekly compose` reads them when `SPONSORS_ENABLED=true`.

## Env-var quick reference

| Var | Used by | Default |
|---|---|---|
| `TOWNLOOPS_API_URL` | all skills that hit TownLoops | `http://localhost:8004` |
| `TOWNLOOPS_API_KEY` | all skills that hit TownLoops | none — required |
| `TOWNLOOPS_TOWN_SLUG` | all skills | none — required |
| `LISTMONK_URL` / `LISTMONK_USER` / `LISTMONK_TOKEN` / `LISTMONK_LIST_ID` | newsletter-weekly | none — required for push |
| `FIRECRAWL_API_KEY` | enrichment (--firecrawl), event-ingest firecrawl sources | none |
| `SPONSORS_ENABLED` | sponsor-management, newsletter template | `false` |
| `AGENT_TZ` | event-ingest date rendering | `America/Los_Angeles` |
| `SOURCES_YAML_PATH` | event-ingest, event-source-discovery | `$HERMES_HOME/state/sources.yaml` |
| `TOWNLOOPS_DB_PATH` | event-ingest, enrichment, town-init, newsletter, sponsors | `$HERMES_HOME/state/townloops.db` |
| `DISCOVERY_DB_PATH` | event-source-discovery | `$HERMES_HOME/state/discovery.db` |
| `HERMES_HOME` | all | `/home/agent/.hermes` (docker) or `~/.hermes` |
