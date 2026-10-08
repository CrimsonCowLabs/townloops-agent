# Skill inventory

One-page reference for what each skill does + its most common commands.
For the big picture see `../CLAUDE.md`; for each skill's full contract see its
own `SKILL.md`.

> The repo is **event-first**: events move through a per-event state machine
> (`engine/event_state.py`) drained by a workqueue heartbeat
> (`engine/orchestrator.py:drain_workqueue`). Most pipeline skills are invoked
> by the drainer with `--event-uid <uid>`, one per state transition. Seed
> discovery, find, newsletter, and platform skills are invoked directly (cron
> wrappers under `scripts/`, the dashboard, or ad hoc).

## Event pipeline — state machine

```
found ──extract-metadata──► metadata-extracted ──(operator approves)──► approved
approved ──extract-venue──► venue-derived ──extract-hosts──► hosts-derived
hosts-derived ──extract-people──► people-derived ──enrich-entities──► entities-enriched
entities-enriched ──ship──► shipped                      declined = terminal
                                                          stuck-<state> = operator override
```

`event-find` lands events directly at `metadata-extracted / pending` (the
separate `event-extract-metadata` skill for the `found → metadata-extracted`
step is not built yet — the orchestrator skips its `extract-metadata` action).

## Skills

### Seed discovery & find (LLM, direct-invoked)

| Skill | CLI | When |
|---|---|---|
| `event-seed-discovery` | `discover.py [--limit N] [--dry-run] [--show]` | Weekly (Sun) + first-run. LLM proposes seed URLs → `event_seeds` (status `proposed`). |
| `event-find` | `find.py [--seed-id N] [--limit N] [--dry-run]` | Daily. Scrape approved seeds, LLM-extract events → `metadata-extracted / pending`. |

### Per-event transitions (workqueue-driven, `--event-uid`)

| Skill | Action | From state → to state | Notes |
|---|---|---|---|
| `event-derive-venue` | `extract-venue` | `approved` → `venue-derived` | Match/create shell `venues` row. Exit 2 if no venue signal. |
| `event-derive-hosts` | `extract-hosts` | `venue-derived` → `hosts-derived` | Rule-based host detection; NULL host is legit success. |
| `event-derive-people` | `extract-people` | `hosts-derived` → `people-derived` | Extract performers/creators; NULL is legit success. |
| `event-enrich-entities` | `enrich-entities` | `people-derived` → `entities-enriched` | Researches each linked entity from its own site; fills blanks only (`engine/entity_enrich.py`). |
| `event-ship` | `ship` | `entities-enriched` → `shipped` | POST entities then event (venue→host→people→event). No-op exit 0 if `TOWNLOOPS_API_KEY` unset. |

### Live sync (cron, daily)

| Skill | CLI | When |
|---|---|---|
| `entity-live-sync` | `sync.py run [--limit N] [--kind K] / list / clear --kind K --entity-uid UID` | Daily 03:30. Entities shipped earlier whose local content changed (enrichment) → resync from live, PATCH live blanks, GET-verify (`engine/entity_sync.py`); cross-town rows skipped. `list`/`clear` for operator-flagged 404/mismatch rows. |

### Legacy structured-source pipeline (LLM-free, `sources.yaml`)

| Skill | CLI | When |
|---|---|---|
| `event-ingest` | `ingest.py ingest / ship [--dry-run] [--only-kind …] / resync / kinds / list / list-sources / add-manual / fetch` | Ad hoc only (no cron). Fork of SLT's aggregator (`docs/fork-extraction.md`). |

### Operator (dashboard chat + Telegram)

| Skill | CLI | When |
|---|---|---|
| `operator-status` | `status.py` | Headline event/seed/workqueue counts. |
| `operator-list-events` | `list.py [--status …] [--before ISO] [--source …] [--limit N]` | Filtered event listing. |
| `operator-review-event` | `review.py --query "…" [--limit N]` | Fuzzy single-event lookup (title/venue/uid prefix). |
| `operator-bulk-approve` | `approve.py (--source … \| --before ISO) [--limit N]` | **Proposes** a mass approve; dashboard Confirm widget executes. |
| `operator-bulk-decline` | `decline.py (--source … \| --before ISO) [--status …] [--limit N]` | **Proposes** a mass decline; dashboard Confirm widget executes. |
| `operator-requeue` | `requeue.py --queue-id N` | Reset one stuck workqueue row; heartbeat retries. |

### Platform, newsletter & sponsors

| Skill | CLI | When |
|---|---|---|
| `town-init` | `init.py setup / status` | Once at deploy; anytime for a health check. |
| `townloops-api` | `api.py snapshot / diff / check / describe <res> / query <res> …` | Daily `api-check`; ad-hoc reads. |
| `onboarding-orchestrator` | `orchestrate.py tick / status / finalize` | `tick` drains the workqueue (legacy phase names retained for compat). |
| `newsletter-weekly` | `newsletter.py compose / push / show --week <ISO>` | Compose Sun 08:00 (draft); operator triggers `push` (needs `--yes-really-send`). |
| `sponsor-management` | `sponsors.py add-sponsor / list-sponsors / assign / audit` | Ad-hoc + Mon audit. Gated by `SPONSORS_ENABLED`. |

## Data flow

```
event-seed-discovery  →  event_seeds (proposed)
        ↓ (operator approves in /v2 dashboard)
event-find            →  events (state=metadata-extracted, approval=pending)
        ↓ (operator approves)  events.state = approved
workqueue heartbeat drains, one skill per transition:
  event-derive-venue → event-derive-hosts → event-derive-people
                     → event-enrich-entities → event-ship
        ↓
event-ship            →  TownLoops API (entities then event)
        ↓
newsletter-weekly compose  →  state/newsletters/<week>.html
        ↓ (operator approves)
newsletter-weekly push     →  Listmonk
```

Legacy `event-ingest` runs a parallel `sources.yaml → parse → mine → ship`
path that writes the same `events` + entity tables. Sponsors ride alongside:
`sponsor-management assign` sets `ad_spots`, `newsletter-weekly compose` reads
them when `SPONSORS_ENABLED=true`.

## Scheduling

`cron/jobs.json` is the source of truth (all `no_agent: true` unless noted):

| Job | Schedule | What it runs |
|---|---|---|
| `workqueue-heartbeat` | `*/10 * * * *` | `orchestrator.drain_workqueue()` — the per-event derive/enrich/ship transitions. |
| `event-find-am` / `event-find-pm` | `0 6,18 * * *` | `scripts/event_find.py` → `event-find/find.py` over every approved seed, detached (outlasts Hermes' 120s script timeout; flock-guarded, log in `state/logs/event-find-<date>.log`). |
| `event-seed-discovery-weekly` | `0 4 * * 0` | `event-seed-discovery/discover.py discover --limit 15`. |
| `api-check` | `15 4 * * *` | `townloops-api/api.py check`. |
| `nightly-digest` | `0 21 * * *` | `orchestrator.summary(hours=24)` → report + Telegram. |
| `newsletter-compose` | `0 8 * * 0` | LLM job (Telegram): compose next week's draft. |
| `newsletter-push-reminder` | `0 20 * * 0` | LLM job (Telegram): nudge operator to push. |
| `sponsor-audit` | `0 10 * * 1` | LLM job (Telegram): placement/expiry audit. |

The engine-level jobs (`workqueue-heartbeat`, `nightly-digest`, `event-find-am/pm`) run the cron
wrappers under `scripts/`, mounted at `/home/agent/.hermes/scripts` in both the
root and per-town composes (`PYTHONPATH=/app`, so their `from engine import ...`
resolves). Those same wrappers (`workqueue_heartbeat.py`, `onboarding_summary.py`,
`event_find.py`, `event_seed_discovery.py`, `event_collect.sh`) are the
entrypoints for non-cron / bare-systemd use. `event-ingest` (legacy
`sources.yaml` path) is ad-hoc only — no cron job.

## Env-var quick reference

| Var | Used by | Default |
|---|---|---|
| `TOWNLOOPS_API_URL` | all skills that hit TownLoops | `http://localhost:8004` |
| `TOWNLOOPS_API_KEY` | all skills that hit TownLoops | none — required |
| `TOWNLOOPS_TOWN_SLUG` | all skills | none — required |
| `LISTMONK_URL` / `LISTMONK_USER` / `LISTMONK_TOKEN` / `LISTMONK_LIST_ID` | newsletter-weekly | none — required for push |
| `FIRECRAWL_API_KEY` / `FIRECRAWL_API_URL` | event-find, event-seed-discovery, enrichment, discovery | none (self-host via URL, or cloud via key) |
| `SPONSORS_ENABLED` | sponsor-management, newsletter template | `false` |
| `AGENT_TZ` | event-ingest, newsletter | `America/Los_Angeles` |
| `SOURCES_YAML_PATH` | event-ingest | `$HERMES_HOME/state/sources.yaml` |
| `TOWNLOOPS_DB_PATH` | all skills touching state | `$HERMES_HOME/state/townloops.db` |
| `TOWN_PROFILE_PATH` | workqueue rate limits | `$HERMES_HOME/town-profile.yaml` |
| `HERMES_HOME` | all | `/home/agent/.hermes` (docker) or `~/.hermes` |
