# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone **Hermes agent** that runs the ongoing operations of a **single TownLoops town**:
discover event sources → find & extract events → derive/enrich venues, hosts, people → ship to the
TownLoops API → publish a weekly Listmonk newsletter, plus sponsor management. One clone runs one
town; `TOWNLOOPS_TOWN_SLUG` is single-valued. A second town = a second clone with its own `.env`.

The agent runs on the Nous `hermes-agent` base image, extended by the local `Dockerfile`. Skills are
plain Python CLI scripts bind-mounted into `/home/agent/.hermes/skills/business/`. The LLM provider is
**ChatGPT via the OpenAI Codex CLI** (`config.yaml` → `provider: openai-codex`), not an API key —
`codex login` on the host once; `~/.codex` is mounted into the container so tokens refresh.

## ⚠️ Two pipelines exist — event-first is current

The repo was rewritten from an **entity-first phase machine** (bootstrapping → discovering → verifying
→ steady_state) to an **event-first workqueue** (commits `c3fbf55`, `fd82438`, `103bcf8`). This matters:

- **Current / source of truth**: `engine/event_state.py`, `engine/workqueue.py`, `engine/orchestrator.py`
  (now a workqueue drainer), the `event-find` / `event-seed-discovery` / `event-derive-*` /
  `event-enrich-entities` / `event-ship` skills, `operator-*` skills, and the dashboard `/v2` views
  (`dashboard/db_v2.py`, `dashboard/templates/v2/`).
- **Stale / partially superseded docs**: `README.md`, `SOUL.md` §Mission/Rhythm/Toolset, and
  `docs/skill-inventory.md` still describe the old entity-first skill names (`event-source-discovery`,
  `venue-enrichment`, `business-discovery`, `onboarding-orchestrator` phase machine). `docs/STATUS.md`
  is a cold-start hand-off dated 2026-07-10 and predates the rewrite. **Verify against the actual
  `skills/business/` tree and `engine/` before trusting any skill name in those docs.**

Some old machinery is intentionally kept for backward-compat: `onboarding-orchestrator` still exists,
`orchestrator.run_tick` now just drains the workqueue, `PHASES = ("event-first",)`, and `agent_state`
is still read by the dashboard.

## Architecture

Three layers, repo-root is the Python import root (`pythonpath = .` in `pytest.ini`):

- **`engine/`** — shared library, the single source of truth. No skill logic here; skills import from it.
  - `db.py` — SQLite connect + `run_migrations` (atomic: splits SQL on top-level `;`, runs each
    migration in `BEGIN`/`COMMIT`, records `schema_version`; broken migration → `ROLLBACK` → retried
    next boot). Migrations are **SQLite-specific** (`engine/migrations/NNN_*.sql`, currently through 018).
  - `event_state.py` — the linear event state machine (`found → metadata-extracted → approved →
    venue-derived → hosts-derived → people-derived → entities-enriched → shipped`, plus `declined`
    and `stuck-<state>`). Advancement is **optimistic locking**: `UPDATE ... WHERE state = <old>`;
    rowcount 0 means another worker/operator already moved it and the loser skips silently. This is
    the entire concurrency-safety story.
  - `workqueue.py` — `event_workqueue` table. `enqueue` (idempotent via UNIQUE), `claim` (atomic,
    per-action-per-hour rate limited via `town-profile.yaml` → `workqueue_rate_limits`), `complete`,
    `fail` (after `MAX_ATTEMPTS=3`, marks the event `stuck-` and resolves the row).
  - `orchestrator.py` — `drain_workqueue()`: for each `next_action`, claim ≤5 items, `subprocess`-invoke
    the matching skill with `--event-uid <uid>`, advance state + enqueue the next action on rc 0, `fail`
    on nonzero, and **quietly skip (`rc==127`) skills that aren't implemented yet**. Wrapped in an
    `engine.lock` flock so overlapping heartbeats don't collide.
  - `ingest_core.py` (1055 lines) — extracted shared core for event-ingest: entity dataclasses,
    `ENTITY_REG`, hashing, HTTP/Firecrawl fetch, JSON-LD walker, CSS-selector parsers, `PARSERS`
    registry, enrichment overlay. `skills/business/event-ingest/ingest.py` is a **fork of SLT's
    `south-lake-tahoe-events/events.py`** that imports from this core — see Gotchas.
  - `townloops_api.py` — TownLoops client: envelope unwrap, retry on 429/503/network, deterministic
    town-scoped `Idempotency-Key`, and `verify_town()` (fail-closed cross-town guard).
  - `discovery.py`, `research.py`, `policy.py` (per-day POST caps + Firecrawl token bucket),
    `listmonk_api.py`, `lock.py` (flock advisory locks), `log.py` (JSON structured logging),
    `inspection.py` (writes `agent_activity` for the dashboard ticker), `derive_helpers.py`,
    `entity_enrich.py` (budgeted local → live → new entity resolution, per-kind `SPECS` for all 5
    entity kinds, `enrich_entity` research + schema validation + fill-blanks; event-reconcile and
    event-enrich-entities use it — new enrichment work builds on it, not on a skill script),
    `entity_classify.py` (blank `category_slug` on venues/businesses/communities ← town vocabulary:
    whole-word keyword map, Codex fallback constrained to the town's slugs; run by `enrich_entity`),
    `performer_research.py` (performer/creator evidence ladder: event evidence → venue-page links →
    profile metadata → search as discovery only; bios verbatim or null; ambiguous → `needs-review`;
    driven by the `performer-research` skill, ≤3 per 20-min cron run),
    `entity_sync.py` (local entity changes → live: resync, fill live blanks, verify; used by event-ship and
    `entity-live-sync`), `live_patch.py` (full-body PATCH + GET verify for events and entities),
    `live_lookup.py` (find a live record before creating one).

- **`skills/business/<skill>/`** — one dir per skill, each with `SKILL.md` (Hermes-discovered contract)
  + a Python entry script. Skills are invoked three ways: by cron (`cron/jobs.json`), by the
  orchestrator drainer (`--event-uid`), and by the operator (dashboard chat / Telegram slash commands).
  Event-first skills are keyed to workqueue actions in `orchestrator._ACTION_SKILLS`.

- **`dashboard/`** — FastAPI operator UI (`main.py`, ~1400 lines) on `127.0.0.1:8090`. `db.py` (entity/v1),
  `db_v2.py` (event-first — operator approve/decline is a pure state flip on `events`, picked up by the
  next heartbeat). Bcrypt single-password auth + signed session cookie (`auth.py`). The `/v2` **chat
  panel** (`chat.py`, `chat_worker.py`, `chat_db.py`, `intent_handlers.py`) talks to Hermes over
  `/v1/chat/completions` — see SOUL.md §"Operator chat" for the widget-confirmation contract on
  destructive actions.

### Runtime state (`state/`, a Docker volume — back this up)
- `townloops.db` — events, workqueue, entities, enrichment, newsletter log, `agent_state`, `chat_messages`.
- `discovery.db` — source-discovery candidates.
- `sources.yaml`, `categories.json`, `openapi.json` (+ `.previous.json`), `newsletters/<iso-week>.html`,
  `reports/<date>.md`, `locks/`.

## Commands

```bash
# Tests — run from repo root. ~250 tests, in-process, no network, ~3s.
pytest                              # full suite
pytest tests/test_orchestrator.py   # single file
pytest -k "migration and rollback"  # by keyword
pytest -m integration               # network tests (skipped by default)

# Full stack (agent + bundled Firecrawl: api/playwright/redis/rabbitmq/postgres)
docker compose build                # image MUST exist before hashing dashboard pw
docker compose up -d
docker compose logs -f agent

# Dashboard first-run (order matters — hash helper needs the image)
./bin/dashboard-hash-password       # prompts, prints $2b$12$… → paste into .env (escape $ as $$)
docker compose up -d dashboard      # http://127.0.0.1:8090

# Drive the pipeline by hand (inside the container or with env set)
python engine/orchestrator.py       # not a CLI — call drain_workqueue() from scripts/
python scripts/workqueue_heartbeat.py   # what the 10-min cron runs
python skills/business/event-seed-discovery/discover.py --help
python skills/business/event-find/find.py --dry-run
python skills/business/town-init/init.py setup      # verify creds, snapshot categories, migrate DB
```

Skill CLIs follow a `<script>.py <subcommand> [--flags]` shape; run with `--help` (or read the skill's
`SKILL.md`) for the current verb set — do not trust `docs/skill-inventory.md`, it lists old skills.

## Conventions & gotchas

- **`event-ingest/ingest.py` is a fork.** Before editing it, diff against the upstream SLT source
  (`~/.hermes/skills/business/south-lake-tahoe-events/events.py`). The deterministic town-scoped
  `Idempotency-Key`, `agent` metadata `hermes/{slug}-events`, env-driven path constants, and `AGENT_TZ`
  reads are deliberate edits that MUST be re-applied on any re-fork. Shared logic belongs in
  `engine/ingest_core.py`, not the fork.
- **`verify_town()` is a hard cross-town guard.** Never write to a town other than `$TOWNLOOPS_TOWN_SLUG`.
  It fails closed if the bearer's scope or the `categories.json` snapshot drifts. Called at the top of
  ingest and enrichment sweeps.
- **Enrichment is "fill blanks only"** (`engine/entity_enrich.fill_blanks` writes only NULL-locally fields, to
  protect operator edits) and refreshes `content_hash`. It reaches live via `engine/entity_sync.py`
  (event-ship + the daily `entity-live-sync` sweep): resync from the live row first (live values win), then
  PATCH only live's blanks, GET-verify, and only then set `pushed_hash`. A 404 or a PATCH that doesn't land
  flags the entity (`extra_json.live_sync`) for the operator — never re-created, never retried until cleared.
- **Performer/creator identity is never inferred from a name.** They're researched only by the
  `performer-research` batch (event-enrich-entities skips them); a profile is attached only when a venue page
  links it or its metadata names the town/a linked venue *and* the role. Bios are the profile's own text or
  null — never template copy. Don't route performers back through `entity_enrich`'s name search.
- **Newsletter push is operator-gated.** `newsletter.py push` defaults to preview-only; real send needs
  `--yes-really-send`. Sunday cron only *composes* a draft. Never wire auto-push.
- **`SPONSORS_ENABLED=false` is the safe default** — never flip it in a template. Money placements are
  per-town operator opt-in.
- **Leader/follower for HA.** Two hosts can share a slug; only the leader runs cron (follower binds
  `cron/jobs.empty.json` over `cron/jobs.json` via a gitignored `docker-compose.override.yml`). Server-side
  Idempotency-Key dedup makes duplicate POSTs safe. See README §"Second instance / failover".
- **Codex CLI is best-effort.** If the CLI is missing/throttled, LLM (classifier, event-find extraction)
  calls no-op and retry on the next tick — don't treat a single throttle as a bug.
- **Business discovery for *new* businesses lives on the CMO Hermes**, not here
  (`~/.hermes/skills/business/townloops-business-discovery/`). This repo's enrichment only fills details
  on existing rows.

## Deployment / routing (VPS)

This product follows the CrimsonCowLabs shared-VPS pattern (see `~/projects/crimsoncowlabs/CLAUDE.md`):
the app container + its deploy live here, but **reverse-proxy/TLS/subdomain routing lives in the master
Caddyfile in `peak-to-crumble-web/`** — edit it there and run `./deploy.sh caddy`, never SSH-edit the
VPS Caddyfile. The dashboard binds loopback (`127.0.0.1:8090`); front it with Caddy + TLS and set
`DASHBOARD_COOKIE_SECURE=true`.
