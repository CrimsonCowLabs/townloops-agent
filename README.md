# townloops-agent

A standalone Hermes agent that runs the ongoing operations of a single TownLoops town: event ingest, entity enrichment, weekly Listmonk newsletter, and (once endpoints ship) sponsor / ad-spot management.

> **Running this on a VPS and don't want to read code?** Start with
> **[docs/vps-quickstart.md](docs/vps-quickstart.md)** — a plain-English,
> copy-pasteable guide covering provisioning, secrets, first-town
> onboarding, day-to-day operations, and troubleshooting. The rest of
> this README assumes you're comfortable with Docker + shell.

## What one deployment does

One clone of this repo runs **one town**. `TOWNLOOPS_TOWN_SLUG` is single-valued. For a second town, clone the repo again with a different `.env`.

Every deployment cascades: research sources → ingest events → mine + enrich venues/businesses/performers → publish weekly newsletter.

## Deploy

```bash
git clone <repo> ~/townloops-agent-<town>
cd ~/townloops-agent-<town>

cp .env.example .env
# Fill in: TOWNLOOPS_API_KEY, TOWNLOOPS_TOWN_SLUG, LISTMONK_*, FIRECRAWL_API_KEY,
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENAI_MODEL

docker-compose up -d
docker-compose logs -f agent
```

The first successful boot runs `town-init setup` automatically (verifies credentials, snapshots category taxonomy, migrates the state DB).

The taxonomy classifier (`entity-research`) uses **your ChatGPT account via the OpenAI Codex CLI** — no separate API key. Run `codex login` on the host once; the per-town compose mounts `~/.codex` into the container so tokens can refresh. If the CLI is missing or throttled, classifier calls no-op and are retried on the next hourly orchestrator tick.

## Operator dashboard

Non-technical operators shouldn't need `docker exec`. The per-town compose ships a second service `dashboard` that exposes a web UI on **`http://127.0.0.1:8090`** (bound to loopback — front with nginx/Caddy + TLS for external access).

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

What the dashboard covers:
- **Overview** — phase, tick count, LLM health, per-kind entity counts, source-run stats. Force-tick, sweep, and nightly-digest buttons.
- **Candidates** — filter/paginate `discovery_candidates`; approve/reject inline via htmx. Bulk-approve all new candidates from a given source signal (fixes the 50-row-click problem).
- **Entities** — read-only view of businesses/venues/communities/performers/creators with `pushed_at` timestamps.
- **Sources** — event sources with approve action.
- **Newsletter** — preview current-week HTML draft (sandboxed iframe); "Push to Listmonk" with `yes-push-live` typed-confirmation.
- **Reports** — renders nightly-digest markdown files as HTML.
- **Blocked** — squid egress log tail, grouped by denied host → guides which domains to add to `allowed_hosts` + the squid ACL.

Auth is single-password bcrypt with a signed session cookie (`HttpOnly`, `SameSite=Strict`, 30-day TTL). Set `DASHBOARD_COOKIE_SECURE=true` in `.env` when fronting with TLS.

## Telegram (operator-on-the-go)

If `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USERS` are set in `.env`, the Hermes gateway registers each skill as a slash command. From your phone:

```
/onboarding-orchestrator status     — current phase + tick count
/onboarding-orchestrator tick       — force a tick
/onboarding-orchestrator summary    — nightly digest on demand
/newsletter-weekly show             — see this week's draft
/business-discovery list --status new
/event-source-discovery approve <host>
```

The agent also **outbound-Telegrams** on: phase transitions, LLM healthy↔throttled flips, and the nightly digest (21:00 UTC).

### Without Docker

Copy `systemd/hermes-gateway.service.template` to `~/.config/systemd/user/hermes-gateway-townloops.service`, edit the `WorkingDirectory` and `EnvironmentFile` paths, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-gateway-townloops
```

You must also `pip install jinja2 requests pyyaml firecrawl-py` into the interpreter that runs Hermes.

## Skills

| Skill | Purpose |
|---|---|
| `town-init` | Bootstrap: verify keys, snapshot categories, migrate SQLite. |
| `event-source-discovery` | Build the town's `sources.yaml` — search → probe → review → compile. |
| `event-ingest` | Fetch feeds, mine venues/businesses/performers, POST/PATCH to TownLoops. |
| `venue-enrichment` | Web-research thin venues to fill address/phone/website/image. |
| `business-enrichment` | Same, for businesses (seeded from venue research + manual adds). |
| `performer-enrichment` | Same, for performers (bio, spotify, socials). |
| `newsletter-weekly` | Compose weekly HTML campaign, push to Listmonk. |
| `sponsor-management` | CRUD sponsors + ad_spots. Disabled until TownLoops endpoints ship. |

See `docs/skill-inventory.md` for the full skill catalog and CLI invocations.

## Tests

Run the suite from the repo root:

```
pytest
```

178 tests covering `engine/` — db migrations, ingest_core parsers, townloops_api client, enrichment state machine, research heuristics, lock contention. All in-process, no network, ~3s wall-clock. See `docs/STATUS.md` § "Test suite" for the coverage map.

**Note**: proactive web sweeps for new businesses in a town live on the CMO Hermes at `~/.hermes/skills/business/townloops-business-discovery/`, not in this repo. This repo's `business-enrichment` skill only fills details on existing rows. See `docs/STATUS.md` § "Business discovery" for the split.

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

Tuning knobs (`FIRECRAWL_NUM_WORKERS_PER_QUEUE`, `FIRECRAWL_MAX_CONCURRENT_JOBS`, etc.) are optional and read from the same `.env` — see `.env.macneo-template` for the full list. The API is also published to `127.0.0.1:8302` for debugging with tools like curl from the host.

**Not using the bundled Firecrawl?** Set `FIRECRAWL_API_URL` in `.env` to your target (cloud: `https://api.firecrawl.dev` + `FIRECRAWL_API_KEY=fc-...`; or an external self-host: `http://host.docker.internal:8302` if you have a separate Firecrawl already running natively on the host).

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
