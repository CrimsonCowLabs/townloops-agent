# Status — 2026-07-07

Cold-start hand-off doc. Read this first if you're picking the repo up after a break.

## Where we are

**v1 scaffold complete. Nothing has been run.** All eight skills have SKILL.md + code and the repo has one clean commit (`6fbdcd3`). Python syntax checks pass. No end-to-end run, no docker build, no API contact.

Treat everything below as "wired up but unverified."

## What's real vs. what's stubbed

| Skill | Code | Real behavior | Notes |
|---|---|---|---|
| `town-init` | full | verifies TownLoops + Listmonk creds, snapshots categories, migrates state DB | first thing to run after a fresh deploy |
| `event-source-discovery` | full | add/probe/list/approve/reject/compile | agent drives web search externally |
| `event-ingest` | full (2528-line fork of SLT `events.py`) | fetch → mine → dedupe → ship | untested against a non-slt slug |
| `venue-enrichment` | full | claim → research → PATCH | untested |
| `business-enrichment` | full | " | untested |
| `performer-enrichment` | full | " | untested |
| `newsletter-weekly` | full | compose (Jinja2 → `state/newsletters/`) + push (Listmonk) | template.html has never rendered a real event set |
| `sponsor-management` | full CLI | local DB writes always; remote POST **only if** `SPONSORS_ENABLED=true` | remote endpoints don't exist yet — see `docs/api-sponsor-schema-proposal.md` |

## Verification NOT done

The plan (`/home/aaron/.claude/plans/groovy-shimmying-lighthouse.md`) has a "Verification" section with 7 steps. **None of them have been executed.** In order of increasing blast radius:

1. `docker-compose build` — never run. Base image tag may not exist; skill discovery via bind mount unproven.
2. `town-init setup` against `slt` — read-only smoke test that would prove credential + category flow.
3. `event-ingest ingest --dry-run` with SLT's real sources.yaml copied in.
4. Enrichment sweeps against `slt` (5-row limit) — proves PATCH round-trip.
5. Newsletter compose + push against a test Listmonk list.
6. Discovery end-to-end on a real prospective town.
7. Sponsor gating — confirm `SPONSORS_ENABLED=false` is respected.

These need to happen before the repo can be handed to a client.

## Known limitations / gotchas

### `event-ingest/ingest.py` is a fork
It's a verbatim copy of `~/.hermes/skills/business/south-lake-tahoe-events/events.py` with **only** these edits:
- Path constants read from env (`SOURCES_YAML_PATH`, `TOWNLOOPS_DB_PATH`, `EVENTS_DATA_DIR`, `ENGINE_MIGRATIONS_DIR`).
- `TOWNLOOPS_TOWN_SLUG` is required; no `slt` default.
- Timezone reads from `AGENT_TZ`.
- Shebang changed from a hardcoded venv path to `/usr/bin/env python3`.

**Everything else is byte-identical.** The two copies WILL drift. If SLT gains a parser or fix, it must be manually ported. Before making any edits, run `diff -u ~/.hermes/skills/business/south-lake-tahoe-events/events.py skills/business/event-ingest/ingest.py` to see the delta.

### Migration 005 is SQLite-specific + untested
The `ALTER TABLE` + partial index syntax in `engine/migrations/005_agent_extensions.sql` is SQLite. If a client ever asks for Postgres, this file needs rewriting. Also: never run against a real DB — the specific column additions on existing tables (`venues.enrichment_status`, etc.) have not executed. `db.run_migrations` will apply them on first `town-init setup`.

### Listmonk on `:9090`
User asserted the deployment uses port 9090; Listmonk's default is 9000. When I tried to reach `:9090` during planning it was unresponsive. Confirm listener before trusting.

### Firecrawl fallback is untested
`engine/research.py:firecrawl_fetch` calls `https://api.firecrawl.dev/v1/scrape`. That path is a guess based on the current Firecrawl docs. Verify against a live key before shipping.

### No Dockerfile — depends on base image + pip
`docker-compose.yml` uses `nousresearch/hermes-agent:v2026.5.16` unchanged. The Python packages the skills need — `feedparser`, `requests`, `pyyaml`, `beautifulsoup4`, `python-dateutil`, `icalendar`, `jinja2`, `firecrawl-py` (optional) — are assumed to already live in that image. If any are missing, add a `Dockerfile` extending the base with a `pip install` step.

### Enrichment semantics: "fill blanks only"
`engine/enrichment.py:apply` only writes into fields that are currently NULL locally. This is deliberate — protects operator edits — but has a subtle failure mode: if an operator edits a value **on TownLoops** without also updating the local row, the local row still looks blank, so the next sweep fills it (potentially with worse data) and PATCHes it back. **Mitigation:** always operator-edit via a local ship, or add a `resync` pass that pulls remote → local before sweeping. Neither is built yet.

### No test suite
Zero unit tests. The plan didn't call for them, but before the second client deploys, at least add pytest coverage on:
- `engine/townloops_api.Client._request` retry + error unwrap
- `engine/enrichment.claim` / `apply` state transitions
- `engine/research.detect_kind` on a fixture set

### Time zones
`AGENT_TZ` is only read by `event-ingest/ingest.py`. `newsletter-weekly` hardcodes `-08:00` for the Monday-morning send default. If we deploy for an east-coast town, that's wrong. **Fix before an east-coast client.**

### The docker-compose volume mounts assume /home/agent
`HERMES_HOME=/home/agent/.hermes` in `docker-compose.yml` is based on my read of the base image's default user. If the actual image uses a different UID/user, the bind mounts land in the wrong place. Verify with `docker-compose run agent whoami` first.

## Concrete TODOs

Order matches likely priority.

1. **Run `docker-compose build` + `up -d`.** Fix whatever breaks. Expect one of: image tag doesn't exist, missing pip deps, skill discovery not wired.
2. **Run `town-init setup` against SLT** (read-only). This proves credential flow, category snapshot, and the migration runner.
3. **Copy SLT's `sources.yaml` into `state/`** and run `event-ingest ingest`. Expect the same event count SLT gets. Any diff = fork regression.
4. **Sponsor endpoints on TownLoops.** Blocked on user. Once shipped, mint a sponsor-scope token, flip `SPONSORS_ENABLED=true`, and re-run `sponsors.py assign` end-to-end.
5. **First real town launch** — pick a target town, run `event-source-discovery seed → probe → review → compile → event-ingest → enrichment → newsletter compose (dry)`. Do NOT push the newsletter until subscriber list is confirmed.
6. **Timezone fix in newsletter push default** — parametrize the `-08:00` hardcoded offset in `newsletter.py:cmd_push`.
7. **Add a `Dockerfile`** if any pip deps are missing from the base image.
8. **Add a `resync-remote-to-local` command** to prevent the enrichment overwrite failure mode described above.
9. **Add unit tests** for the highest-risk modules (`townloops_api`, `enrichment`, `research.detect_kind`).

## Considerations for weeks-from-now-me

- **Don't trust the SLT fork.** Before touching `event-ingest/ingest.py`, always diff against the current SLT source. There's no automatic drift detection.
- **The sponsor schema in `docs/api-sponsor-schema-proposal.md` is MINE, not the user's.** Before implementing the TownLoops backend, get user review. Fields, enums, and placements are all opinionated guesses.
- **The user runs a lot of Hermes agents.** Don't confuse this repo's layout with the profile pattern under `~/.hermes/profiles/<name>/`. See the memory note `project_townloops_agent.md` for the distinction.
- **Listmonk on `:9090`** — that's a non-default port. If the deployment moves or a second Listmonk shows up, the URL changes but everything else stays.
- **`SPONSORS_ENABLED=false` is the safe default.** Never flip it in a template `.env.example`.
- **Skill discovery is via docker bind mount, not `skills.external_dirs`.** If someone tries to deploy via bare systemd, they need to either recreate the bind-mount layout in `~/.hermes/skills/` or switch `config.yaml` to declare `skills.external_dirs`.
- **The `event-ingest ship` command shares a bearer token with all resource POSTs.** If the client's token scope narrows to just events, the mining ship will 403 on venues/businesses/etc. Confirm token scope covers all six resources when minting.
- **Weekly Sunday cron only DRAFTS the newsletter.** The push is operator-triggered by design. If you rewire it to auto-push, remember to remove the SOUL.md "MUST ask about" clause first.

## Files worth re-reading before major changes

- `docs/skill-inventory.md` — quick surface map.
- `docs/api-sponsor-schema-proposal.md` — the schema I'm assuming.
- `SOUL.md` — the agent's identity + constraints.
- `/home/aaron/.claude/plans/groovy-shimmying-lighthouse.md` — the approved plan; deviations from it should be recorded in this STATUS doc.
- Memory: `/home/aaron/.claude/projects/-home-aaron-workspace-hermes/memory/project_townloops_agent.md`.
