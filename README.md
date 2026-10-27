# townloops-agent

A standalone Hermes agent that runs the ongoing operations of a single TownLoops town: event seed discovery → find → derive/enrich venues, hosts, and people → ship to TownLoops, plus a weekly Listmonk newsletter and sponsor / ad-spot management.

> **Running this on a VPS and don't want to read code?** Start with
> **[docs/vps-quickstart.md](docs/vps-quickstart.md)** — a plain-English,
> copy-pasteable guide covering provisioning, secrets, first-town
> onboarding, day-to-day operations, and troubleshooting. The rest of
> this README assumes you're comfortable with Docker + shell.

## What one deployment does

One clone of this repo runs **one town**. `TOWNLOOPS_TOWN_SLUG` is single-valued. For a second town, clone the repo again with a different `.env`.

Every deployment cascades: research sources → ingest events → mine + enrich venues/businesses/performers → publish weekly newsletter.

## Deploy

**Operators: follow [docs/vps-quickstart.md](docs/vps-quickstart.md).** That is the one documented
deployment: one repo checkout, `bin/townloops-agent-add <slug>` scaffolding a folder per town under
`~/townloops-agents/`, and the shared Firecrawl + Squid egress stacks in `templates/_shared/`.

The repo-root `docker-compose.yml` below is the **single-town developer setup** (a clone per town,
Firecrawl bundled, no egress proxy) — for working on the agent, not for running towns.

```bash
git clone <repo> ~/townloops-agent-<town>
cd ~/townloops-agent-<town>

cp .env.example .env
# Fill in: TOWNLOOPS_API_KEY, TOWNLOOPS_TOWN_SLUG, LISTMONK_*,
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DASHBOARD_PASSWORD
# (The LLM is ChatGPT via `codex login` on the host — no key in .env.)

docker-compose up -d
docker-compose logs -f agent
```

Before `up`, run `town-init setup` (snapshots the category taxonomy, migrates the state DB, then runs `town-init preflight` — one read-only verdict that exits non-zero when the town can't work). Re-run `preflight` first whenever something stops.

The LLM steps (`event-seed-discovery`, `event-find`) use **your ChatGPT account via the OpenAI Codex CLI** — no separate API key. Run `codex login` on the host once; the per-town compose mounts `~/.codex` into the container so tokens can refresh. If the CLI is missing or throttled, those calls no-op and are retried on the next scheduled run.

## Operator dashboard

Non-technical operators shouldn't need `docker exec`. The per-town compose ships a second service `dashboard` that exposes a web UI on **`http://127.0.0.1:8090`** (bound to loopback — front with nginx/Caddy + TLS for external access).

The dashboard is for **supervised** towns only. An autonomous town (`operating_mode: autonomous` in `town-profile.yaml`) doesn't run it — the TownLoops admin moderation queue is its only review step. `bin/townloops-agent-add <slug> --autonomous` scaffolds such a town with a `docker-compose.override.yml` that keeps the service from starting (`services: {dashboard: {profiles: ["supervised-dashboard"]}}`), and the dashboard itself refuses to start (non-zero exit, logged reason) when the profile says autonomous.

**First-run bootstrap** (order matters — the hash helper needs the image):

```bash
cd ~/townloops-agents/<slug>
docker compose build                    # image must exist before hashing
../../workspace/townloops-agent/bin/dashboard-hash-password
# → prompts for password, prints $2b$12$... hash
# Paste into .env as DASHBOARD_PASSWORD=... (escape any literal $ as $$)
docker compose up -d dashboard
open http://127.0.0.1:8090              # log in with the plaintext password
```

The dashboard has two views:

- **`/v2` — event-first (current).** Workqueue depth, event counts by approval/state, in-flight/shipped/stuck panels, seed approval (proposed → approved), per-event approve/decline, "Run event-find" / "Discover seeds" / requeue actions, and the **Hermes chat panel** (talks to the agent over `/v1/chat/completions`; renders Confirm widgets for bulk approve/decline — see `SOUL.md` §"Operator chat").
- **`/` — legacy entity-first.** Overview, Candidates, Entities, Sources — still mounted for the older discovery flow.

Shared panels:
- **Newsletter** — preview current-week HTML draft (sandboxed iframe); "Push to Listmonk" with typed confirmation.
- **Reports** — renders nightly-digest markdown files as HTML.
- **Blocked** — squid egress log tail, grouped by denied host → guides which domains to add to `allowed_hosts` (then `bin/townloops-agent-add --egress` + reload Squid, or let `bin/townloops-egress-sync` on the operator's crontab do both — it also applies the hosts a town's agent adds itself via `egress-hosts`; see docs/vps-quickstart.md).

Auth is single-password bcrypt with a signed session cookie (`HttpOnly`, `SameSite=Strict`, 30-day TTL). Set `DASHBOARD_COOKIE_SECURE=true` in `.env` when fronting with TLS.

## Telegram (operator-on-the-go)

If `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USERS` are set in `.env`, the Hermes gateway registers each skill as a slash command. From your phone:

```
/operator-status                    — event / seed / workqueue counts
/operator-list-events --status pending
/onboarding-orchestrator tick       — force a workqueue drain
/event-seed-discovery --show        — current seeds
/newsletter-weekly show             — see this week's draft
```

The agent also **outbound-Telegrams** on: shipped-event cycles, LLM healthy↔throttled flips, and the nightly digest.

### Without Docker

Copy `systemd/hermes-gateway.service.template` to `~/.config/systemd/user/hermes-gateway-townloops.service`, edit the `WorkingDirectory` and `EnvironmentFile` paths, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-gateway-townloops
```

You must also `pip install jinja2 requests pyyaml firecrawl-py` into the interpreter that runs Hermes.

## Skills

The event pipeline is **event-first**: a per-event state machine
(`found → metadata-extracted → approved → detail-read → venue-derived → hosts-derived →
people-derived → entities-enriched → shipped`) drained by a workqueue
heartbeat. Each state transition is one skill invoked with `--event-uid`.
See `engine/event_state.py` for the machine and `docs/skill-inventory.md`
for the full catalog + CLI invocations.

**Event-first pipeline (seed-driven, LLM):**

| Skill | Workqueue action | Purpose |
|---|---|---|
| `event-seed-discovery` | — | LLM proposes seed source URLs (chamber, city, library, venues) → `event_seeds` for operator approval. |
| `event-find` | — | Scrape every approved seed, LLM-extract events → land at `metadata-extracted / pending`. |
| `event-derive-venue` | `extract-venue` | Match or create a shell `venues` row from the event's venue signal. |
| `event-derive-hosts` | `extract-hosts` | Identify the hosting business/community (NULL is legit). |
| `event-derive-people` | `extract-people` | Extract performers/creators referenced in the event. |
| `event-enrich-entities` | `enrich-entities` | Research linked entities from their own sites; fill NULL fields only. |
| `event-ship` | `ship` | POST derived entities then the event to TownLoops (venue → host → people → event). |

**Legacy structured-source pipeline (LLM-free, `sources.yaml`):**

| Skill | Purpose |
|---|---|
| `event-ingest` | Parse configured feeds (iCal/JSON-LD/RSS/HTML/Firecrawl), mine venues/businesses/communities/performers/creators, dedupe, POST/PATCH deltas to TownLoops. Fork of SLT's aggregator; see `docs/fork-extraction.md`. |

**Operator (dashboard chat + Telegram):**

| Skill | Purpose |
|---|---|
| `operator-status` | Headline event / seed / workqueue counts. |
| `operator-list-events` | Filtered event listing (status / date / source). |
| `operator-review-event` | Fuzzy single-event lookup by title / venue / uid prefix. |
| `operator-bulk-approve` / `operator-bulk-decline` | **Propose** a mass approve/decline — read-only; execution requires a dashboard Confirm widget (see `SOUL.md` §"Operator chat"). |
| `operator-requeue` | Reset one stuck workqueue row so the heartbeat retries it. |

**Platform, newsletter & sponsors:**

| Skill | Purpose |
|---|---|
| `town-init` | Bootstrap: snapshot categories, migrate SQLite, `preflight` (read-only: will this town work?). |
| `townloops-api` | Snapshot + breaking-change check of the TownLoops OpenAPI; freeze frozen resources; read-through queries. |
| `onboarding-orchestrator` | `tick` now drains the workqueue (legacy phase-machine names retained for compat). |
| `newsletter-weekly` | Compose weekly HTML campaign, push to Listmonk (operator-gated). |
| `sponsor-management` | CRUD sponsors + ad_spots. Live against TownLoops endpoints; gated by `SPONSORS_ENABLED`. |

**Scheduled runs** (`cron/jobs.json`, all `no_agent` unless noted): `workqueue-heartbeat` drains the per-event derive/enrich/ship queue every 10 min (detached, bounded by `town-profile.yaml` → `workqueue_drain`; `workqueue_heartbeat.py --drain` runs one drain in the foreground); `event-find-am/pm` scrape approved seeds at 06:00/18:00; `event-seed-discovery-weekly` (Sun 04:00) proposes new seeds; `api-check` (04:15) snapshots the OpenAPI contract; `nightly-digest` (21:00) reports event/workqueue state; and the LLM-backed `newsletter-compose` (Sun 08:00), `newsletter-push-reminder` (Sun 20:00), and `sponsor-audit` (Mon 10:00) deliver over Telegram. The engine-level jobs run the cron wrappers under `scripts/` (mounted at `/home/agent/.hermes/scripts`); those same wrappers (`workqueue_heartbeat.py`, `onboarding_summary.py`, `event_find.py`, `event_seed_discovery.py`) are also the standalone entrypoints for non-cron / bare-systemd use.

## Tests

Run the suite from the repo root:

```
pytest
```

~250 tests covering `engine/` — db migrations, event state machine, workqueue claim/rate-limiting, ingest_core parsers, townloops_api client, enrichment, research heuristics, lock contention. All in-process, no network, ~3s wall-clock.

**Note**: proactive web sweeps for new businesses run in this repo as the `business-discovery` skill (Firecrawl sweeps + page verify, deduped against live TownLoops businesses). It only proposes candidates for the operator — it never writes to TownLoops. See `docs/business-discovery-handoff.md`.

## Sponsors

The TownLoops API ships sponsor + ad-slot endpoints (`/towns/{slug}/sponsors` and `/towns/{slug}/ad-slots` — see `openapi.json`). The `sponsor-management` skill talks to them directly; see `skills/business/sponsor-management/SKILL.md` for payload shapes, local→API field mapping, and CLI. Still gated by `SPONSORS_ENABLED=false` in `.env` — flip to `true` after minting a sponsor-scope token. The old proposal doc lives at `docs/archive/api-sponsor-schema-proposal.md` for history only.

## State

Runtime data lives in `./state/` (docker volume). SQLite databases:
- `state/townloops.db` — entities, enrichment queue, sponsor mirrors, newsletter log.
- `state/discovery.db` — source discovery candidates and probes.
- `state/sources.yaml` — compiled feed list, input to `event-ingest`.
- `state/categories.json` — snapshot of the town's platform category taxonomy.
- `state/newsletters/<iso-week>.html` — composed campaign bodies.

Back this up. It represents the agent's memory of what's already shipped to TownLoops.

## Second instance / failover

Two hosts can point at the same `TOWNLOOPS_TOWN_SLUG` — the ship path's Idempotency-Key is payload-deterministic (`ingest.py:_bulk_post`), so duplicate POSTs dedupe server-side, and each host has its own `state/townloops.db`. The design principle is **leader/follower**: one host runs the cron; the other is a warm on-demand runner. Prevents duplicate source fetches / newsletter drafts.

### Per-host mechanics

Both hosts share the same repo (rsync or git). Per-host differences live entirely in `docker-compose.override.yml` (gitignored):

```yaml
# ~/workspace/townloops-agent/docker-compose.override.yml   [macneo — follower]
services:
  agent:
    hostname: townloops-agent-macneo
    volumes:
      - ./cron/jobs.empty.json:/home/agent/.hermes/cron/jobs.json:ro
```

And a sibling file:

```json
// ~/workspace/townloops-agent/cron/jobs.empty.json
{"version": 1, "jobs": []}
```

Thinkpad (leader) needs only the hostname override:

```yaml
# ~/workspace/townloops-agent/docker-compose.override.yml   [thinkpad — leader]
services:
  agent:
    hostname: townloops-agent-thinkpad
```

The hostname surfaces in `enrichment_queue.claimed_by` and every stderr line via `engine/enrichment._worker_tag()`, so logs from the two instances never blur.

### Firecrawl is bundled

The `docker-compose.yml` in this repo includes the full Firecrawl stack (api, playwright, redis, rabbitmq, nuq-postgres) using pullable images. `docker compose up -d` brings the whole thing up in one step. The agent reaches Firecrawl via internal Docker DNS at `http://firecrawl-api:3002` (the default set in the compose file — leave `FIRECRAWL_API_URL` unset in your `.env` to use it).

Tuning knobs (`FIRECRAWL_NUM_WORKERS_PER_QUEUE`, `FIRECRAWL_MAX_CONCURRENT_JOBS`, etc.) are optional overrides read from `.env`; their defaults live inline on the `firecrawl-api` service in `docker-compose.yml` (`${FIRECRAWL_…:-default}`). The API is also published to `127.0.0.1:8302` for debugging with tools like curl from the host.

**Local Firecrawl only.** The agent never calls cloud Firecrawl: with no local URL configured, Firecrawl calls fail (`FirecrawlNotConfigured`) and a URL pointing at `firecrawl.dev` is refused (`engine/firecrawl_env.py`). **Not using the bundled stack?** Set `FIRECRAWL_API_URL` in `.env` to another local instance, e.g. `http://host.docker.internal:8302` for one running natively on the host. `FIRECRAWL_API_KEY` is only needed if that instance checks keys.

**Running as a native Hermes profile (no Docker)?** Hermes scrubs every `FIRECRAWL_*` variable from commands the agent runs, so set `TOWNLOOPS_FIRECRAWL_URL` instead (it takes precedence over `FIRECRAWL_API_URL`; see `engine/firecrawl_env.py`). Without it, agent-invoked skills have no Firecrawl at all (they never fall back to cloud).

### rsync from leader → follower

```
rsync -av --delete \
  --exclude='state/' --exclude='.env' --exclude='__pycache__/' \
  --exclude='.venv/' --exclude='*.pyc' --exclude='.pytest_cache/' \
  --exclude='.git/' --exclude='docker-compose.override.yml' \
  --exclude='cron/jobs.empty.json' \
  /path/to/townloops-agent/ user@follower:/path/to/townloops-agent/
```

`state/` and the per-host files are protected. `.env` on the follower is authored fresh (never rsynced).

### Failover swap

When the leader dies or is being retired:

1. On the (former) follower: delete `docker-compose.override.yml` (or replace the empty-cron bind), then `docker compose restart agent`.
2. On the (former) leader — if still alive: add the empty-cron override there and restart. Now it's the follower.

No state migration is required — the first `event-ingest ingest` on the new leader is idempotent-forward (server-side dedup handles anything the old leader had already shipped). Warm-up cost is roughly one ingest cycle.

### Concurrency risks not fixed by leader/follower

- **Manual `resync` on both hosts racing** — convention only ("resync from leader"). No lock. If someone runs it on both simultaneously, both will do the work; TownLoops-side dedup handles POSTs but `pushed_hash` alignment could momentarily flap.
- **Server-side "current runner" token** — the "right" fix that would let both hosts run cron freely. Requires TownLoops API work. Out of scope here.
