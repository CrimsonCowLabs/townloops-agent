# Status — 2026-07-10

## Second-instance deploy (macneo, in progress)

2026-07-10: standing up a second townloops-agent instance on macneo (macOS + OrbStack). Same repo, same `TOWNLOOPS_TOWN_SLUG=slt`. Concurrency safety via **leader/follower** cron split — thinkpad = leader, macneo = follower via a `docker-compose.override.yml` that binds `cron/jobs.empty.json` over `cron/jobs.json`. Server-side Idempotency-Key dedup (P0 fix) handles any duplicate POSTs; per-host `state/townloops.db` prevents enrichment-queue races. **Firecrawl now bundled** in the same compose project (5 services: api / playwright / redis / rabbitmq / nuq-postgres via pullable ghcr.io/firecrawl images) — `docker compose up -d` brings the whole thing up; agent reaches it via `firecrawl-api:3002` in the shared network. See README § "Second instance / failover" for the swap procedure.

### Base-image discoveries during first-ever `docker compose build` (2026-07-10)

Three real bugs surfaced when finally running `docker compose build` (item #1 on the pre-existing verification-not-done list). All fixed in the shared repo:

1. **Base image is uv-only, no pip.** `nousresearch/hermes-agent:v2026.5.16` ships Python 3.13.5 + `uv` at `/usr/local/bin/uv`, no `pip` anywhere (not even `python3 -m pip`). Dockerfile now uses `uv pip install --system --break-system-packages`. The `--break-system-packages` flag is needed because the system Python is PEP 668 externally-managed.
2. **Old `./skills:/home/agent/.hermes/skills:ro` mount masked bundled skills.** Base image tries to seed 87 bundled skills (`apple/`, `creative/`, `autonomous-ai-agents/`, `mcp/`, etc.) into `/home/agent/.hermes/skills/`. When our repo mounted `./skills` read-only OVER that whole path, every seed failed with `Errno 30: Read-only file system`. Now we mount narrowly at `./skills/business:/home/agent/.hermes/skills/business:ro` — bundled skills coexist. **Unblocks Mac-ecosystem work**: `apple-notes`, `apple-reminders`, `findmy`, `imessage`, `macos-computer-use` are all bundled and now available on macneo without any custom skill authoring.
3. **`host.docker.internal:host-gateway` added.** `docker-compose.yml` now includes an `extra_hosts:` entry so the agent container can reach services (Firecrawl, Listmonk) running on the host from inside the container. Works identically on Linux + OrbStack.

All three fixes apply to any future host deployment, thinkpad included. Effective when the thinkpad instance is stood up.

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
| `sponsor-management` | full CLI | local DB writes always; remote POST **only if** `SPONSORS_ENABLED=true` | remote endpoints landed 2026-07-08. Payload shapes in `sponsor-management/SKILL.md` + `openapi.json`. |

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
**Fork extraction complete (Phase 1: 2026-07-08, Phase 2: 2026-07-09).** `engine/ingest_core.py` is now the single source of truth for the dataclasses, ENTITY_REG, text/venue utils, timezone helpers, identity/content hashing, HTTP fetch layer, JSON-LD walker, CSS-selector helpers, top-level parsers + `PARSERS` registry, and enrichment overlay. `ingest.py` shrank from 2627 → 1737 lines (−890, 34%) and just imports what it needs from the shared core. See `docs/fork-extraction.md` for how SLT's `events.py` can adopt the same core.

Before extraction, `ingest.py` was a verbatim copy of `~/.hermes/skills/business/south-lake-tahoe-events/events.py` with these deliberate edits:
- Path constants read from env (`SOURCES_YAML_PATH`, `TOWNLOOPS_DB_PATH`, `EVENTS_DATA_DIR`, `ENGINE_MIGRATIONS_DIR`).
- `TOWNLOOPS_TOWN_SLUG` and `TOWNLOOPS_API_URL` are both required (fail-fast via `_require_env`).
- Timezone reads from `AGENT_TZ`.
- Shebang changed from a hardcoded venv path to `/usr/bin/env python3`.
- **2026-07-08 P0 fixes**: `Idempotency-Key` is now deterministic and town-scoped (POST hashes sorted batch uids; PATCH uses `content_hash`; DELETE resync-orphan uses `hermes-{town_slug}:resync-orphan:{kind}:{slug}`). The `agent` metadata is now `hermes/{TOWNLOOPS_TOWN_SLUG}-events`. These MUST be re-applied if the file is ever re-forked from SLT.

The two copies WILL drift. Before making any edits, run `diff -u ~/.hermes/skills/business/south-lake-tahoe-events/events.py skills/business/event-ingest/ingest.py` to see the delta. P2 TODO `extract engine/ingest_core.py` is the permanent fix.

### Migration 005 is SQLite-specific (verified applies cleanly)
The `ALTER TABLE` + partial index syntax in `engine/migrations/005_agent_extensions.sql` is SQLite-only. **Applies cleanly** — the P1.3 atomic-migration work (2026-07-08) exercises all migrations 001-007 end-to-end on a fresh SQLite DB and confirmed schema_version is populated after each. A Postgres port would need rewriting; unblocked but out of scope until a Postgres-preferring client shows up.

### Listmonk on `:9090`
User asserted the deployment uses port 9090; Listmonk's default is 9000. When I tried to reach `:9090` during planning it was unresponsive. Confirm listener before trusting.

### Firecrawl fallback is untested
`engine/research.py:firecrawl_fetch` calls `https://api.firecrawl.dev/v1/scrape`. That path is a guess based on the current Firecrawl docs. Verify against a live key before shipping.

### Dockerfile added 2026-07-08
`Dockerfile` extends `nousresearch/hermes-agent:v2026.5.16` with the pip deps skills need — `feedparser`, `requests`, `pyyaml`, `beautifulsoup4`, `python-dateutil`, `icalendar`, `jinja2`, `lxml`, `firecrawl-py`. `docker-compose.yml` builds from it (`image: townloops-agent:local`). Pull-only deploys are now unsupported — always `docker-compose build`. If a skill's boot fails on `ModuleNotFoundError`, add the missing package to the `pip install` line in the Dockerfile and rebuild.

### Enrichment semantics: "fill blanks only"
`engine/enrichment.py:apply` only writes into fields that are currently NULL locally. This is deliberate — protects operator edits — but has a subtle failure mode: if an operator edits a value **on TownLoops** without also updating the local row, the local row still looks blank, so the next sweep fills it (potentially with worse data) and PATCHes it back. **Mitigation:** always operator-edit via a local ship, or add a `resync` pass that pulls remote → local before sweeping. Neither is built yet.

### No test suite
Zero unit tests. The plan didn't call for them, but before the second client deploys, at least add pytest coverage on:
- `engine/townloops_api.Client._request` retry + error unwrap
- `engine/enrichment.claim` / `apply` state transitions
- `engine/research.detect_kind` on a fixture set

### Time zones
Both `event-ingest/ingest.py` and `newsletter-weekly/newsletter.py` now read `AGENT_TZ` (2026-07-08). Newsletter's Monday-07:00 send default is computed via `zoneinfo`, so DST and non-Pacific towns are handled correctly.

### The docker-compose volume mounts assume /home/agent
`HERMES_HOME=/home/agent/.hermes` in `docker-compose.yml` is based on my read of the base image's default user. If the actual image uses a different UID/user, the bind mounts land in the wrong place. Verify with `docker-compose run agent whoami` first.

## Concrete TODOs

Priority tiers from the 2026-07-08 review (findings + verified bugs).
Format: `[status] title — file:line — why`. Status: `[ ] pending`, `[x] done`, `[~] partial`.

### P0 — sharp edges (will bite in production)

- [x] **Idempotency-Key is randomized per attempt** — `skills/business/event-ingest/ingest.py`. Bulk POST now uses `f"hermes-{TOWNLOOPS_TOWN_SLUG}:{kind}:post:{sha1(sorted_uids)}"` — deterministic per batch, retries dedupe server-side. PATCH uses `content_hash` (naturally distinct per edit). DELETE (`_delete_townloops_row`) gets town-scoped too. Fixed 2026-07-08.
- [x] **Hardcoded town slug in `agent` metadata** — `skills/business/event-ingest/ingest.py:_cmd_flush` — now `f"hermes/{TOWNLOOPS_TOWN_SLUG}-events"`. Fixed 2026-07-08.
- [x] **Newsletter default send time hardcoded to Pacific** — `skills/business/newsletter-weekly/newsletter.py:cmd_push` — now builds `datetime(monday, 7, 0, tzinfo=ZoneInfo(AGENT_TZ)).isoformat()`. Handles DST + non-Pacific towns correctly. Fixed 2026-07-08.
- [x] **README claims sponsors are blocked (they're not)** — `README.md` Sponsors section rewritten to point at `sponsor-management/SKILL.md` + `openapi.json`. `docs/api-sponsor-schema-proposal.md` moved to `docs/archive/`. Fixed 2026-07-08.
- [x] **`TOWNLOOPS_API_URL` fallback split-brain** — `skills/business/event-ingest/ingest.py` — now uses `_require_env("TOWNLOOPS_API_URL")` (same fail-fast pattern as `TOWNLOOPS_TOWN_SLUG`). `engine/townloops_api.py:50`'s `KeyError` is unchanged; both now fail loudly. Fixed 2026-07-08.

### P1 — correctness, drift, silent failures

- [x] **Entity payloads missing social + image fields the API now accepts** — migration 007 adds `image_path`/`image_alt`/`instagram`/`facebook`/`tiktok`/`twitter`/`youtube`/`linkedin` to `businesses`+`communities`, and `facebook`/`tiktok`/`twitter`/`youtube`/`linkedin` to `performers`, and `facebook`/`tiktok`/`twitter`/`linkedin` to `creators`. `Business`/`Community`/`Performer`/`Creator` dataclasses + `ENTITY_REG` cols extended. `engine/research._SOCIAL_HOSTS` map recognizes all six socials in `sameAs` blocks (facebook.com/tiktok.com/x.com/linkedin.com/youtube.com etc.). `enrichment.SPECS.fillable` extended for business+performer. `enrichment.apply` mirrors `youtube` ↔ `youtube_url` so per-kind fillable picks the right spelling. Fixed 2026-07-08.
- [x] **Firecrawl failures swallowed silently, no cost meter** — `engine/research.firecrawl_fetch` now: (a) honors `FIRECRAWL_API_URL` (self-hosted support, matches the CMO fix), (b) logs every non-2xx to stderr with body sample, (c) raises new `FirecrawlAuthError` on 401/403 so a bad key doesn't silently degrade the whole sweep, (d) increments a `_STATS` meter exposed via `firecrawl_stats()`. `enrichment.sweep` diffs the meter across the run and includes the delta in its JSON output so operators see call/error counts per sweep. Fixed 2026-07-08.
- [x] **Migration atomicity** — `engine/db.run_migrations` now calls a new `_apply_one_migration()` that splits SQL on top-level `;` (respecting quoted string literals + `--` comments), runs everything inside `BEGIN`/`COMMIT`, and records `schema_version` inside the same transaction. Broken migration → `ROLLBACK` → no partial schema, no `schema_version` row → next boot cleanly retries. Verified with a synthetic broken migration test. Fixed 2026-07-08.
- [x] **Cron re-entrancy for hung jobs** — new `engine/lock.py` provides `fcntl.flock`-based advisory locks under `$TOWNLOOPS_LOCK_DIR` (defaults to `$HERMES_HOME/state/locks/`). `enrichment.sweep()` wraps claim+apply in `acquire("enrichment-<kind>")` and gracefully skips on `LockHeld` (exits with a `skipped: true` summary rather than piling on). `event-ingest ingest` wraps `_cmd_ingest_locked()` in `acquire("event-ingest-<town_slug>")`. Kernel releases the lock even on crash. All three enrich.py wrappers routed through the new `enrichment.sweep()` helper so they inherit the guard uniformly. Fixed 2026-07-08.
- [x] **Newsletter push has no `--dry-run` / confirmation guard** — `newsletter.py push` now defaults to preview-only. Without `--yes-really-send` it prints a JSON summary of what WOULD be scheduled (week, planned send-at, subject, listmonk list id, body byte count) and exits 0 without touching Listmonk. Typo in `--week` costs nothing. Fixed 2026-07-08.
- [x] **Envelope-unwrap fragility in Listmonk client** — `newsletter.py:cmd_push` now trusts `listmonk_api.Client._req`'s existing envelope unwrap (it already returns `body.get("data", body)`). Missing `id` on the returned dict is treated as a hard error with a helpful stderr message + exit 3, not a silent `None` cascade into `schedule_campaign(None, ...)`. Fixed 2026-07-08.

### P2 — architecture & maintainability

- [x] **`event-ingest/ingest.py` is a fork of SLT's `south-lake-tahoe-events/events.py`** — Phase 1 (2026-07-08) + Phase 2 (2026-07-09) both landed. `engine/ingest_core.py` now holds: the seven entity dataclasses, `ENTITY_REG`, text/venue utils, `AGENT_TZ`-backed timezone helpers, identity + content hashing, the HTTP fetch layer (`_http_get`, `_firecrawl_endpoint`, `_firecrawl_scrape`, `_detail_links_from_html`), the JSON-LD walker + Event synthesis (`_walk_jsonld`, `_jsonld_to_event`, `_extract_jsonld_from_html`), CSS-selector helpers (`_text`, `_attr_or_text`, `_extract_selectors_from_html`), all five top-level parsers + the `PARSERS` registry, and the enrichment overlay. `ingest.py` shrank from **2627 → 1737 lines (−890, 34%)**; `ingest_core.py` is 1051 lines. Unused-import cleanup done. Full end-to-end smoke tests pass. See `docs/fork-extraction.md` for the SLT-side adoption path — Phase 3 (entity mining) is deferred and only worth doing if `_mine_entities` becomes a drift hotspot.
- [x] **Enrichment scripts are 1-line-different copies** — shared CLI extracted to `engine/enrich_cli.py`. Each of `skills/business/{venue,business,performer}-enrichment/enrich.py` is now an 8-line wrapper that calls `enrich_cli.run("<kind>")`. Bug fixes and new subcommands (see `resync` + `cross-check` below) land once, not three times. Fixed 2026-07-08.
- [x] **`engine/townloops_api.Client.bulk_entries` is defined but unused** — deleted 2026-07-08. `event-ingest`'s ship loop has its own retry + Idempotency-Key + 207 Multi-Status handling. If bulk becomes shared logic, rebuild all three pieces here rather than reviving the stub.
- [x] **CMO discovery/research handoff is undocumented from this repo's side** — `docs/business-discovery-handoff.md` written; explains ownership boundary, escalation triggers, `TOWNLOOPS_RESEARCH_DRAFTS_DB` env var, and the docker-compose bind-mount recipe. New `business-enrichment cross-check` subcommand (in `enrich_cli.py`, gated to `kind == "business"`) reads the CMO's drafts.db read-only and lists unshipped business drafts with `--town-hint <substring>` address filter. Smoke-tested against the real CMO drafts.db (50 unshipped drafts surface correctly). Fixed 2026-07-08.
- [x] **Enrichment overwrite risk — no `resync --from-remote`** — `engine/enrichment.resync_from_remote(kind, limit)` GETs each shipped row from TownLoops, copies remote non-null values into the local row, and aligns `pushed_hash` with `content_hash` so the ship logic doesn't spuriously PATCH the sync back. Exposed as `enrich.py resync [--limit N]` on all three enrichment wrappers. Protected columns (bookkeeping like `entity_uid`, `pushed_at`, `source*`) are never overwritten. Fixed 2026-07-08.
- [x] **No structured logging** — `engine/log.py` provides `get_logger(name)` returning a `townloops.<name>` logger with a JSON formatter (ts, level, name, town, msg, plus any structured `extra`). Configuration is idempotent, one-shot at first use. `$TOWNLOOPS_LOG_LEVEL` overrides level; `$TOWNLOOPS_LOG_FILE` adds a file sink alongside stdout. Applied to `engine/research.py` Firecrawl error paths and `skills/business/newsletter-weekly/newsletter.py` warning/error paths as exemplars. Other skills adopt incrementally. Fixed 2026-07-08.
- [x] **Test suite** — landed 2026-07-09. **178 tests, 2.9s wall-clock**, zero external dependencies (no network, no shared filesystem beyond `tmp_path`). Covers:
  - `engine/db.py` — `_split_sql` edge cases (comments, quoted semicolons), `DEFAULT_DB_PATH` derivation from `$HERMES_HOME`, migration atomicity + rollback on partial failure (P1.3).
  - `engine/ingest_core.py` — every text/venue util, `_parse_dt` with tz, all four hashing functions, `Event.dedupe_key`, `ENTITY_REG` shape invariants including per-P1.1 socials presence.
  - `engine/ingest_core.py` parsers — `_walk_jsonld`, `_jsonld_to_event` with organizer/performer/creator variants, `_extract_jsonld_from_html`, `_extract_selectors_from_html` (regex, name_exclude, venue overrides, relative-link resolution), `_detail_links_from_html`, `_extract_enrichment_field` (string + dict specs), `_apply_enrichments` end-to-end with mocked fetch.
  - `engine/townloops_api.py` — envelope unwrap, 429/503/network-error retries, exhausted retries → `TownloopsError`, error extraction on 4xx, Idempotency-Key propagation, `verify_town` in all four regimes (P3.2).
  - `engine/enrichment.py` — `claim`/`apply`/`ship` state transitions, `resync_from_remote` protected-column guarantee + `pushed_hash` alignment (P2.4), sweep skip-on-LockHeld + Firecrawl stats delta.
  - `engine/research.py` — `detect_kind` heuristics across JSON-LD types + og:type + domain fallbacks, Firecrawl error surfacing (P1.2) including `FirecrawlAuthError` on 401/403, self-hosted URL semantics.
  - `engine/lock.py` — acquire/release, cross-process contention (fork-based), `_lock_dir` env resolution.

  **Bugs the tests caught during authoring** (both fixed 2026-07-09):
    - `_walk_jsonld` double-yielded every item under `@graph` (redundant traversal — silently deduped downstream but doubled parser work per page).

  Suite invoked via `pytest` from repo root — see `pytest.ini`. Coverage tools not installed yet; target ≥60% for the next client deploy.

### P3 — polish, unverified assumptions, doc cleanup

- [x] **`docker-compose.yml` assumes** an image tag / user path that may not exist — `Dockerfile` added 2026-07-08 that extends the base with `feedparser`, `requests`, `pyyaml`, `beautifulsoup4`, `python-dateutil`, `icalendar`, `jinja2`, `lxml`, `firecrawl-py`. `docker-compose.yml` now builds locally. Still test `docker-compose run agent whoami` before trusting the bind-mount paths.
- [x] **Systemd `HERMES_HOME` mismatch** — fixed 2026-07-08. `engine/db.py` derives `DEFAULT_DB_PATH` from `$HERMES_HOME` (matches every other module in the repo), falling back to `/home/agent/.hermes/state/townloops.db` only if unset. Systemd template's `HERMES_HOME` now points at the repo root (`/home/CLIENT/townloops-agent`) instead of the state subdir. `.env.example` documents that path overrides are optional now that defaults derive from `$HERMES_HOME`.
- [x] **Migration 005 SQLite-specific + untested** — verified applies cleanly during P1.3 atomic-migration testing (2026-07-08). See `## Known limitations / gotchas > Migration 005 is SQLite-specific`. Postgres port remains out of scope until a Postgres client shows up.
- [x] **Move `docs/api-sponsor-schema-proposal.md` to `docs/archive/`** — moved 2026-07-08 alongside the README rewrite. Both STATUS.md references updated.
- [x] **Cross-town leak has no code-level guard** — `engine/townloops_api.Client.verify_town()` added 2026-07-08. Fetches live categories and asserts (a) non-empty result for `$TOWNLOOPS_TOWN_SLUG` (typo/auth issues fail fast), (b) `state/categories.json` snapshot slugs are a subset of live (extra snapshot slugs → cross-town drift → refuse to proceed with a clear error). Cached per-process. Called at the top of `event-ingest ingest` and `enrichment.sweep`. Verified with unit tests covering all three regimes (no snapshot / matching snapshot / drifted snapshot). Fixed 2026-07-08.
- [x] **Deterministic slug scoping** — fixed 2026-07-08. `_slug_for_new_post(row, kind)` (new POST path) emits `f"hermes-{TOWNLOOPS_TOWN_SLUG}-{uid[:16]}"`; `_slug_for_kind()` retains the legacy `f"hermes-{uid[:16]}"` format for PATCH-URL fallback on rows that predate slug capture (backward compat). Dead helper `_townloops_slug_for` deleted.

### Verification steps (still pending from the original plan)

- [ ] Run `docker-compose build` + `up -d`. Fix whatever breaks (image tag, pip deps, skill discovery).
- [ ] Run `town-init setup` against SLT (read-only). Proves credential flow + category snapshot + migration runner.
- [ ] Copy SLT's `sources.yaml` into `state/` and run `event-ingest ingest`. Compare event count to SLT's — any diff = fork regression.
- [ ] First real town launch — `event-source-discovery seed → probe → review → compile → event-ingest → enrichment → newsletter compose (dry)`. Do NOT push the newsletter until the subscriber list is confirmed.
- [x] **Sponsor endpoints on TownLoops.** Landed (`/towns/{slug}/sponsors` + `/towns/{slug}/ad-slots` per `openapi.json`). Migration 006 adds `image_alt`; `sponsors.py` payload builders translate local→API. **Next step**: mint a sponsor-scope token, flip `SPONSORS_ENABLED=true`, and run `sponsors.py add-sponsor` (with `--image-alt`) + `sponsors.py assign` end-to-end against SLT.

## Considerations for weeks-from-now-me

- **Don't trust the SLT fork.** Before touching `event-ingest/ingest.py`, always diff against the current SLT source. There's no automatic drift detection.
- **Superseded on 2026-07-08**: the sponsor schema I proposed in `docs/archive/api-sponsor-schema-proposal.md` is history only. The live API contract is in `openapi.json` (`SponsorPayload`, `AdSlotPayload`) and `sponsor-management/SKILL.md`.
- **The user runs a lot of Hermes agents.** Don't confuse this repo's layout with the profile pattern under `~/.hermes/profiles/<name>/`. See the memory note `project_townloops_agent.md` for the distinction.
- **Listmonk on `:9090`** — that's a non-default port. If the deployment moves or a second Listmonk shows up, the URL changes but everything else stays.
- **`SPONSORS_ENABLED=false` is the safe default.** Never flip it in a template `.env.example`.
- **Skill discovery is via docker bind mount, not `skills.external_dirs`.** If someone tries to deploy via bare systemd, they need to either recreate the bind-mount layout in `~/.hermes/skills/` or switch `config.yaml` to declare `skills.external_dirs`.
- **The `event-ingest ship` command shares a bearer token with all resource POSTs.** If the client's token scope narrows to just events, the mining ship will 403 on venues/businesses/etc. Confirm token scope covers all six resources when minting.
- **Weekly Sunday cron only DRAFTS the newsletter.** The push is operator-triggered by design. If you rewire it to auto-push, remember to remove the SOUL.md "MUST ask about" clause first.

## Business discovery

Active web sweeps for candidate businesses (outside the event-ingest mining side-effect) live on the CMO Hermes, not here:

- **Skill**: `~/.hermes/skills/business/townloops-business-discovery/` — sweep → verify → promote. Promotes verified candidates into the sibling `townloops-research` skill, which is what actually POSTs the business record via `/towns/{slug}/businesses`.
- **Why on CMO, not per-town**: web-wide sweeping is a central operator function, not a per-town operational loop. Every town this repo runs still benefits — the CMO skill takes `--town <slug>` and reads its category list from this repo's live `/categories` endpoint.
- **This repo's business surface stays the same**: `event-ingest` still mines businesses as an event side-effect, and `business-enrichment` still fills details on existing rows. Neither scans the web for new discoverable businesses.

If discovery breaks or needs improvement, edit the CMO skill — do not add a discovery skill here.

## Files worth re-reading before major changes

- `docs/skill-inventory.md` — quick surface map.
- `docs/archive/api-sponsor-schema-proposal.md` — historical proposal, superseded by the live OpenAPI schema.
- `SOUL.md` — the agent's identity + constraints.
- `/home/aaron/.claude/plans/groovy-shimmying-lighthouse.md` — the approved plan; deviations from it should be recorded in this STATUS doc.
- Memory: `/home/aaron/.claude/projects/-home-aaron-workspace-hermes/memory/project_townloops_agent.md`.
