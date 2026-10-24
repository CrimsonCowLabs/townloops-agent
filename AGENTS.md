# AGENTS.md

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
- **Stale / partially superseded docs**: `README.md` and `docs/skill-inventory.md` still describe
  the old entity-first skill names (`event-source-discovery`,
  `venue-enrichment`, `onboarding-orchestrator` phase machine). (`business-discovery` exists again, rebuilt
  on the current engine — see Gotchas.) `docs/STATUS.md`
  is a cold-start hand-off dated 2026-07-10 and predates the rewrite. **Verify against the actual
  `skills/business/` tree and `engine/` before trusting any skill name in those docs.**

Some old machinery is intentionally kept for backward-compat: `onboarding-orchestrator` still exists,
`orchestrator.run_tick` now just drains the workqueue, `PHASES = ("event-first",)`, and `agent_state`
is still read by the dashboard.

## Architecture

Three layers, repo-root is the Python import root (`pythonpath = .` in `pytest.ini`):

- **`engine/`** — shared library, the single source of truth. No skill logic here; skills import from it.
  - `db.py` — SQLite connect + `run_migrations` (atomic: splits SQL on top-level `;`, runs each
    migration in `BEGIN IMMEDIATE`/`COMMIT`, records `schema_version`; broken migration → `ROLLBACK` → retried
    next run; a file another process applied first is skipped). Pending migrations run at the start of every cron
    entry point: the heartbeat drain (`run_migrations`) and each DB-writing `scripts/*.py` wrapper (`db.migrate()`;
    new wrappers call it too). Every DB-touching docker no_agent job in `cron/jobs.json` runs a wrapper,
    never a skill directly; `tests/test_cron_migrate.py` enforces both. Migrations are **SQLite-specific** (`engine/migrations/NNN_*.sql`, currently through 028).
  - `event_state.py` — the linear event state machine (`found → metadata-extracted → approved →
    detail-read → venue-derived → hosts-derived → people-derived → entities-enriched → shipped`, plus `declined`
    and `stuck-<state>`). Advancement is **optimistic locking**: `UPDATE ... WHERE state = <old>`;
    rowcount 0 means another worker/operator already moved it and the loser skips silently. This is
    the entire concurrency-safety story.
  - `workqueue.py` — `event_workqueue` table. `enqueue` (idempotent via UNIQUE), `claim` (atomic,
    per-action-per-hour rate limited via `town-profile.yaml` → `workqueue_rate_limits`), `complete`,
    `fail` (after `MAX_ATTEMPTS=3`, marks the event `stuck-` and resolves the row).
  - `orchestrator.py` — `drain_workqueue()`: for each `next_action`, claim ≤5 items, `subprocess`-invoke
    the matching skill with `--event-uid <uid>`, advance state + enqueue the next action on rc 0, `fail`
    on nonzero, and **quietly skip (`rc==127`) skills that aren't implemented yet**. Wrapped in an
    `engine.lock` flock so overlapping heartbeats don't collide. The heartbeat cron wrapper detaches the
    drain (like `event_find.py`), so a drain may outlast Hermes' 120s script budget; it bounds itself via
    `town-profile.yaml` → `workqueue_drain` (`drain_settings`: ≤`read_detail_per_drain` detail-page
    fetches, started only in the first half of `budget_seconds`; no skill starts once the budget is spent;
    held claims are released unattempted). Clamped so a drain ends before the 15-min stale-claim reaper.
  - `ingest_core.py` (1055 lines) — extracted shared core for event-ingest: entity dataclasses,
    `ENTITY_REG`, hashing, HTTP/Firecrawl fetch, JSON-LD walker, CSS-selector parsers, `PARSERS`
    registry, enrichment overlay. `skills/business/event-ingest/ingest.py` is a **fork of SLT's
    `south-lake-tahoe-events/events.py`** that imports from this core — see Gotchas.
  - `townloops_api.py` — TownLoops client: envelope unwrap, retry on 429/503/network, deterministic
    town-scoped `Idempotency-Key`, and `verify_town()` (fail-closed cross-town guard).
  - `egress.py` — `render_squid_conf([(slug, allowed_hosts)])` → the whole shared `squid.conf` (pure, no I/O):
    fixed `PLATFORM` block (OpenAI/ChatGPT, TownLoops, Telegram, Listmonk) + per town one `acl town_<slug>` and
    its `http_access allow`, `deny all` last; entries → `.host` form, a scheme/path/port/wildcard, an IP literal or
    a public suffix (`PUBLIC_SUFFIXES`, `co.uk`-shaped ccTLD levels) is an `EgressError` naming town + entry;
    sorted, byte-identical on rerun. `towns_from(base_dir)` reads the profiles (non-slug folders like `slt.bak`,
    `_egress` skipped with a note; a non-mapping profile is an error naming the file); `main` (`python -m
    engine.egress BASE_DIR`, what `townloops-agent-add --egress` runs) writes `BASE_DIR/_egress/squid.conf`.
  - `discovery.py` (Firecrawl sweep/verify kernel, no LLM, no DB), `business_discovery.py` (the
    `business-discovery` skill's candidate store on `discovery_candidates` + dedup against live businesses:
    same website/under its path, or name ≥0.85 → `already-live`; live venues too, reason "matches live venue <slug>"),
    `own_site.py` (the one "own page, not a page about it" rule: platform profile, a host carrying the name, else a
    homepage where the caller says one counts — performer research's presence test and discovery's verify; also
    `UTILITY_PATH`, a site's jobs/member-application/deals pages, which discovery always rejects, and `ARTICLE_PATH`,
    the one editorial-path rule — blog/post/article/visitor guide — for discovery's link filter and `roundup_reason`),
    `listing_hosts.py` (the one directory/listing/aggregator host list — restaurantji, Yelp, maps, ticketing,
    Facebook events: evidence at most, never an entity's `website`; enrichment, discovery's blocklist, promote and
    performer research all check it (the listing-website audit too) — extend it, don't add another), `research.py`, `policy.py` (per-day POST caps + Firecrawl token bucket),
    `listmonk_api.py`, `lock.py` (flock advisory locks), `log.py` (JSON structured logging),
    `inspection.py` (writes `agent_activity` for the dashboard ticker), `derive_helpers.py` (also the one
    `extra_json` parser `parse_extra_json`, and `seed_ref`/`seed_name_of` for the `seed:<name>` reference),
    `entity_enrich.py` (budgeted local → live → new entity resolution, per-kind `SPECS` for all 5
    entity kinds, `enrich_entity` research + schema validation + fill-blanks; event-reconcile and
    event-enrich-entities use it — new enrichment work builds on it, not on a skill script),
    `page_extract.py` (quote-checked LLM extraction from a page's text: per-field `FieldHint`, the LLM copies
    sentences, only ones on the page that pass the field's check survive, in page order; a failed call is
    `None` = not looked at. `entity_enrich.PAGE_FIELDS` uses it for a community's `meeting_info` and a venue's
    `accessibility_notes` (venues also read the site's own accessibility/visit/FAQ page when the homepage links
    one — `entity_enrich.access_page`; a bare "ADA compliant"/web-accessibility footer doesn't count) — a failed
    or throttled call defers the entity: its other site fields fill, its status stays `pending` for a re-read;
    `event_detail`'s accessibility quotes share its checks; `find_quote` is the one quote-on-page check — event
    judgement, event people and ticket prices use it too),
    `entity_classify.py` (blank `category_slug` on venues/businesses/communities ← town vocabulary:
    whole-word keyword map, Codex fallback constrained to the town's slugs; run by `enrich_entity`),
    `performer_research.py` (performer/creator evidence ladder: event evidence → venue-page links →
    profile metadata → search as discovery only; bios verbatim or null; a verified own site's profile links (anchors +
    `sameAs`, profile paths only, one per platform) fill blank socials; ambiguous → `needs-review`;
    driven by the `performer-research` skill, ≤3 per 20-min cron run),
    `entity_sync.py` (local entity changes → live: resync, fill live blanks, verify; used by event-ship and
    `entity-live-sync`; a 404 naming a merge survivor re-points instead of flagging), `live_patch.py`
    (full-body PATCH + GET verify for events and entities),
    `entity_ship.py` (one entity → live: lookup, create or adopt, sync; event-ship and business promote),
    `live_lookup.py` (find a live record before creating one; the one parser of a merged entity's 404
    survivor, `merged_into`/`merged_into_slug`, and `follow_merges` — ≤`MAX_MERGE_HOPS`, under `get_live`
    and event-ingest's PATCH. Entity reads by cached slug go through it and re-point via
    `entity_sync.repoint` (slug + `live_id` together): a 404 *with* a survivor re-points, only one without
    means gone/re-POST. Only entity 404s name a survivor (townloops ADR 0009) — an event's never does),
    `event_dedup.py` (cross-seed dedup: `canonical_event_uid` on every new event via `upsert_event`;
    one copy stays pending, the rest are declined `status='duplicate'`; primary beats syndicator seeds),
    `event_cancel.py` (vanish detection in event-find → `possibly-cancelled`, and still-listed events the
    listing marks cancelled/postponed — quote-checked; live → flagged, not shipped → declined; operator-confirmed only,
    marked live as `Cancelled: ` title + `cancelled` tag by the `event-cancellations` skill),
    `feed_probe.py` (machine-readable feed detection from a seed page's HTML — iCal, Google Calendar,
    Tribe REST, Showare, EventCalendarApp, POINT, JSON-LD, events RSS, HTML calendar, hidden Wix calendar —
    recorded on `event_seeds.feed_*` by `event-seed-discovery discover`/`probe`; no extra fetches),
    `seeds.py` (seed writes — feed record, status change, adapter (`set_adapter`: declared config + a dry run that
    must return usable events before it saves; `generic` reverts; subject `seed-adapter` Decisions; releases an
    `awaiting-adapter` hold in the same transaction), and the maintenance verbs `move` (URL verified first —
    an adapter seed through its adapter — `previous_url` kept) / `hold` (status `held` + `hold_reason`: event-find and
    `review` skip it) / `release` (approved only if held from approved) / `restore`, each a recorded Decision; `URL_CHANGES` names the verdicts that change a URL — and the autonomous `review` run: trial a proposed
    seed, re-judge approved ones, look for a dead seed's moved calendar — first `assign_adapters`: a probed `feed_type`
    an adapter reads is dry-run, `autonomy.judge_adapter` assigns (autonomous) or proposes it in the nightly digest
    (supervised — the only step review runs there); network/LLM injected as `Tools`),
    `source_health.py` (every event-find run → `source_runs`; an established seed dropping to zero
    or sharply below its trailing average gets `event_seeds.health_status` + one Telegram/dashboard
    alert, held until it recovers or the operator accepts the new level).
    `blocked_page.py` (the one challenge/error-page detector — `reason(text, status, title)`: HTTP 4xx/5xx,
    Incapsula/Cloudflare/captcha challenge, access-denied, platform 404 page, Showare `no action`, near-empty body,
    named by module constants; markers count on small bodies only. event-find's listing reads and adapter fetches
    (`event_extract.firecrawl_listing`, `seed_adapters._get_json`) → a failed run with that reason, never
    LLM-extracted, never a source-health zero, never a vanish miss; also the seed trial (`trial-not-found`/
    `-challenge`/`-empty-page`) and performer research's unreadable-page check — extend it, don't add another),
    `event_extract.py` (event-find/-reconcile/seed-trial extraction; `published_times` keeps only the times the page
    states — none → date-only: town-local midnight, no end; a Tribe all-day row is date-only too. Stated = in the
    text ("7pm"; "7:00" is 07:00, never 19:00) or an ISO `startDate`/`endDate`/`doorTime` — any other ISO timestamp
    is metadata. `day_and_clock`/`has_clock_time` is the one "has a time" rule (date-only or local midnight = none) —
    ship, reconcile, dedup, autonomy, provenance and `ingest_core._parse_dt` use it. A date-only re-read of a same-seed
    timed row replaces it (`event_dedup`): live → staged as a change, its live end flagged in the digest (PATCH
    can't null `end_at`; `live_patch.flag_unclearable_end`), never a duplicate or a vanished event),
    `seed_adapters.py` (per-seed deterministic reader keyed by `event_seeds.adapter`, NULL = `generic` Firecrawl → LLM;
    its events take event-find's one path, a partial read is a failed run; `tribe`, `showare` (one event per
    performance, paged by `perfCount`) — add an `Adapter` entry to
    `ADAPTERS`: reader, config `params` (`check_config`), the probed `feed_types` it reads (`for_feed`)),
    `event_detail.py` (one recorded read of an approved event's own `source_url` page — the
    `read-detail` action, skill `event-read-detail`: Firecrawl + Codex, `accessibility_notes` kept
    only as quote-checked sentences that name an access feature, else null; skipped with no fetch
    when `source_url` is missing or the seed's listing page; a failed read gives up on the last
    workqueue attempt instead of parking the event. The same pass keeps the stated `price` (quote-checked,
    must state a cost) and ticket/RSVP link `rsvp_url` (on the page, never a homepage); `event_tickets.py`
    turns them into the `free` tag or one `Tickets: …`/`RSVP: …` line atop the shipped description —
    built at ship time, so the local description and content hash never change),
    `event_provenance.py` (#45: an event's `source_url` is the page its fields came from. `listing_events` is the
    one extraction entry point (event-find, event-reconcile, seed trial): LLM JSON → each event read against its own
    listing card (split at extracted titles and at the card's own heading/list-item/table-row structure) — only
    that card's times (`published_times`), a link only if it is the card's own (first after the title, or a linked
    image just above it). Provenance is *established* when the title is on the listing and, if the link had to
    go, the card gives the date — a card with no detail link ships on the listing; an unplaced title or a
    dropped link plus a date its card doesn't give is held. Adapter rows are established by the adapter; read-detail
    still checks any detail URL: a heading must name the event and its section (minus posted/updated lines and
    "other events" lists) give its date. Held via `review_hold` (verdict `autonomy.judge_provenance`). A refused
    link a shipped row carries → `live-mismatch`, digest-listed, live untouched),
    `time_plausibility.py` (#46: times implausible on their face are held, whatever their provenance. The one
    place for the rules and thresholds — `check(start, end)`, pure: pre-dawn town-local start, end before start,
    longer than `MAX_DURATION`; local midnight is date-only (`day_and_clock`), an end with no time on the start's
    own day is no end. `record` after every extraction upsert (event-find, event-reconcile — so a staged change to
    a live row is held before ship), `gate` before read-detail and ship. Held via `review_hold` (verdict
    `autonomy.judge_times`, digest "Held — implausible time"); an approval after the hold accepts *those* times —
    new times are checked afresh, a source that corrects them releases the hold),
    `review_hold.py` (the one hold-for-review lifecycle; a `Kind` per reason — `PROVENANCE`, `TIMES` — each keeping
    its rules/evidence in its own module; a third reason is one more `KINDS` entry. Held = pending at
    `metadata-extracted` + `extra_json.<key>.status` `held-for-review` in both modes, approval cleared
    (`event_state.reopen_for_review`); `hold` is False when the event moved on first — the caller exits
    `RC_DEFERRED` so the drain never advances it (read-detail, ship). One note part per kind (`<prefix>…`, joined
    ` | `; `with_note`/`without_note`/`with_base` — no kind or reconcile note clobbers another). `NOT_HELD_SQL`
    (any kind): the autonomous judge and bulk approval skip held events (operator-bulk-approve, the
    `approve_events` widget — each is approved on its own; only an approval after the hold accepts it); `held()`
    — pending, upcoming by default — for the digest and `autonomy accept|decline <uid>`),
    `series.py` (TownLoops Series, ADR 0007 — the agent never writes one, the client refuses: `series_key`
    = TownLoops' title normalization, pinned to python-slugify; the `state/series.json` snapshot; `decide_title`
    — the title event-ship creates an occurrence under so it auto-links into the right Series (Series title only
    at the Series' one venue, "<Performer> at <Venue>" for a performer-only title that is a Series or recurs
    elsewhere); `shared_fill` — blank Series-shared fields from siblings, never date/price/cancellation;
    `candidates` — the weekly `series-candidates` report), `series_sync.py` (mirror `series_slug`/`series_id`
    from TownLoops onto local rows; fill Series-shared blanks live — event-ship + entity-live-sync),
    `event_groups.py` (collapse a listing on `series_slug` — never title; a singleton keys on its own slug/uid;
    the earliest occurrence represents the group; `SeriesDetails` = `GET /series/{slug}` once per run; `hub_url`
    only for an active Series with a `url`. The newsletter, nightly digest, upcoming audit and
    `operator-list-events` all group through it — new listings do too),
    `autonomy.py` (the autonomy policy — the one place an operator's call is made by the agent:
    `settings()` reads `operating_mode` + the `autonomy:` block fresh each call; event judgement
    `judge_items` — deterministic checks, then one page-quote-checked LLM call per seed run;
    seed trial/trust/role/retire/adapter; candidate, stuck-event, cancellation and Series-streak decisions;
    every `Decision(verdict, reason, evidence)` logged to `agent_activity` worker `autonomy`;
    supervised mode returns `await-operator`. `GatedClient` wraps creates: submission gate
    (pause / circuit / pending cap → `SubmissionsHeld`) and the fail-closed `pending` check;
    records `submissions`), `moderation.py` (the hourly poll: published / rejected / merged
    (re-point) / gone, seed trust, rejection-rate breaker, remembered org rejections; the digest's
    autonomous section, built and formatted there), `event_people.py` (host + performers/creators from one quote-checked
    Codex pass over the event's own text, cached on the event; derive-hosts/-people use it),
    `upcoming_audit.py` (read-only next-N-town-local-days audit for the `operator-audit-upcoming`
    skill: days with no live event, per-day Series/one-off coverage, local events not live yet, sparse live metadata
    (one finding per Series), venues of upcoming events with no image; LIST/GET only),
    `listing_website_audit.py` (read-only, for the `operator-audit-listing-websites` skill: venues/businesses/
    communities whose `website` is a listing page per `listing_hosts.is_listing_url`, local ↔ live paired by
    listing slug → `live_id` → merge-following GET (no re-point); one row per entity, `side` local/live/both,
    `live_unread` when the GET failed, a shared live row counted once (`duplicate_of`) — the listing-website
    cleanup works from `find_listing_websites`' rows + `unverified`; LIST/GET only),
    `source_attribution_audit.py` (read-only, for the `operator-audit-source-attribution` skill, #47: live events
    whose `source_url` is another event's page — each distinct page read once via local Firecrawl (cap
    `--max-fetches`; a cached read-detail page first) and judged by `event_provenance.page_disagreements`, the one
    predicate read-detail holds on (live title + town-local date; no venue check); seed listing pages and blank URLs
    skipped, an unread page `unverified`, never a mismatch; #45's local `live-mismatch` flags folded in; LIST only),
    `listing_website_cleanup.py` (the one-off `listing-website-cleanup` skill — operator-run `apply`, no cron; ledger
    `listing_website_clears`, optimistic status moves, entity writes under the `workqueue-drain` lock. Supervised:
    `clear_listing_websites` widget → `apply`: `live_patch.clear_entity_fields` (`website: ""` + GET verify) first,
    then local. Autonomous: local cleared, a live listing website `flagged` (never PATCHed — TownLoops has no entity
    change submission yet) and listed in the nightly digest until a later `apply` sees live fixed. Local clear keeps
    the URL in `extra_json.listing_pages`, takes live's own site or blanks + `pending` → `reenrich`; `live_unread`
    rows never cleared; each row says `changed` live/local/both/none),
    `preflight.py` (`town-init preflight` + the end of `town-init setup`: one read-only verdict, `Finding`s of
    severity `error`/`warning`/`info` + stable `code` + file-and-key + consequence; exit non-zero on any error.
    healthz + one `list(kind, limit=1)` + `verify_town()`, profile `query_groups` slugs vs the snapshot
    (`policy.has_category`; stale snapshot vs bad profile), mode coherence (autonomous ⇒ override parks the
    dashboard, supervised ⇒ `DASHBOARD_PASSWORD`), open `autonomy_state` circuit verbatim (DB opened `mode=ro`),
    Firecrawl via `firecrawl_env`, Codex login presence, `AGENT_TZ`, Listmonk/Telegram as warnings. Never
    writes or probes by create — a new check stays read-only and cheap).

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
- `sources.yaml`, `categories.json`, `series.json` (Series snapshot), `openapi.json` (+ `.previous.json`), `newsletters/<iso-week>.html`,
  `reports/<date>.md`, `locks/`.

## Commands

```bash
# Tests — run from repo root. ~1300 tests, in-process, no network, ~5 min.
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
python scripts/workqueue_heartbeat.py   # what the 10-min cron runs (detaches; log: state/logs/workqueue-heartbeat-<date>.log)
python scripts/workqueue_heartbeat.py --drain   # one drain in the foreground
python skills/business/event-seed-discovery/discover.py --help
python skills/business/event-find/find.py --dry-run
python skills/business/town-init/init.py setup      # snapshot categories, migrate DB, then preflight
python skills/business/town-init/init.py preflight  # read-only verdict; exit 1 on any error (--json)
```

Skill CLIs follow a `<script>.py <subcommand> [--flags]` shape; run with `--help` (or read the skill's
`SKILL.md`) for the current verb set — do not trust `docs/skill-inventory.md`, it lists old skills.

## Conventions & gotchas

- **`event-ingest/ingest.py` is a fork.** Before editing it, diff against the upstream SLT source
  (`~/.hermes/skills/business/south-lake-tahoe-events/events.py`). The deterministic town-scoped
  `Idempotency-Key` (a PATCH's names its target slug — TownLoops replays on key + body, not path),
  `agent` metadata `hermes/{slug}-events`, env-driven path constants, `AGENT_TZ` reads, and merge-survivor
  re-pointing via `engine/live_lookup.follow_merges` + `entity_sync.repoint` are deliberate edits that MUST be re-applied on any re-fork. Shared logic belongs in
  `engine/ingest_core.py`, not the fork.
- **`verify_town()` is a hard cross-town guard.** Never write to a town other than `$TOWNLOOPS_TOWN_SLUG`.
  It fails closed if the bearer's scope or the `categories.json` snapshot drifts. Called at the top of
  ingest and enrichment sweeps.
- **Enrichment is "fill blanks only"** (`engine/entity_enrich.fill_blanks` writes only NULL-locally fields, to
  protect operator edits) and refreshes `content_hash`. It reaches live via `engine/entity_sync.py`
  (event-ship + the daily `entity-live-sync` sweep): resync from the live row first (live values win), then
  PATCH only live's blanks, GET-verify, and only then set `pushed_hash`. A listing-page `website` is copied neither
  way (nor into a live mirror) — clearing one is the operator-gated `listing-website-cleanup`, never a sync. A 404 naming a curator's merge
  survivor (`merged_into_slug`) re-points the row there (slug + `live_id` persisted) and syncs the survivor; any
  other 404 or a PATCH that doesn't land flags the entity (`extra_json.live_sync`) for the operator — never
  re-created, never retried until cleared.
- **Performer/creator identity is never inferred from a name.** They're researched only by the
  `performer-research` batch (event-enrich-entities skips them); a profile is attached only when a venue page
  links it or its metadata names the town/a linked venue *and* the role. Bios are the profile's own text or
  null — never template copy. Don't route performers back through `entity_enrich`'s name search. A bare one
  (name only) research hasn't verified is never created live (`performer_research.awaits_research`, checked in
  `entity_ship.ship_entity`): the event ships without it, and `entity-live-sync` ships it once released.
- **Series are read-only; `series_slug` is the recurrence key.** Membership comes only from TownLoops'
  auto-link at create (title key vs active Series) or a curator, so the agent controls only the title it
  ships (`series.decide_title`). Live lookup order is slug → same-town-local-date occurrence of the predicted
  Series (`?series=`) → `source_url` ±12h. Reconcile treats a live row with a `series_slug` as recurring (a new
  date is `new`, never a reschedule). `events.series_slug/series_id` are written only from TownLoops responses.
- **One canonical event per real event; cancellations are operator-gated.** `event_uid` is per seed;
  `canonical_event_uid` (engine/event_dedup) groups copies across seeds, and event-ship never creates
  a copy whose sibling is already live. An event missing from all its sources, or still listed but marked
  cancelled on the listing (quote-checked; not-yet-shipped copies are declined in autonomous mode, flagged in supervised), is only *flagged*
  (`events.cancel_status`); it's marked cancelled on TownLoops only after the operator confirms
  (`confirm_cancellations` widget → `event-cancellations apply`). Never wire auto-cancel.
- **Source-health alerts fire on transitions only.** `engine/source_health.record_run` is the one
  place event-find records a seed run (dry runs aren't recorded); failed runs and unparseable
  extractions are never judged — only an explicit empty list is a zero — and a standing
  alert is held against the average it dropped from (`health_baseline`) so a lasting drop neither
  re-alerts nor clears itself. A recorded `feed_url` is evidence for the source-automation review,
  not a switch — event-find still scrapes the seed URL unless the seed has an adapter (`set-adapter`).
- **Autonomous mode is a per-town switch; supervised stays the default.** `operating_mode:
  autonomous` (town-profile.yaml) moves every local gate into `engine/autonomy.py` and makes the
  TownLoops admin moderation queue the only human gate: creates go through `GatedClient` and
  must come back `pending` (a `published` answer opens the circuit and stops all submissions —
  the town needs an `api_ingest` key without publish permission). New entities are submitted pending
  too (TownLoops entity gate, townloops ADR 0009; `autonomy.entity_moderation: false` holds them, and
  events then ship with the venue's name only). Changes to published
  values (date/venue, cancellations) are pending submissions for the merge flow, never PATCHes;
  fill-blank PATCHes still go live. Exit 75 (`workqueue.RC_DEFERRED`) from a workqueue skill = "not now" (claim released, no
  attempt). Only the operator closes the circuit (`autonomy resume`). Supervised-only proposers
  answer `inactive`. Gate code paths on `autonomy.settings()` / `is_autonomous()` — never cache it.
  Tests drive whole towns in-process through `tests/autonomy_town.py`.
- **Newsletter push is operator-gated.** `newsletter.py push` defaults to preview-only; real send needs
  `--yes-really-send`. Sunday cron only *composes* a draft. Never wire auto-push.
- **`SPONSORS_ENABLED=false` is the safe default** — never flip it in a template. Money placements are
  per-town operator opt-in.
- **Leader/follower for HA.** Two hosts can share a slug; only the leader runs cron (follower binds
  `cron/jobs.empty.json` over `cron/jobs.json` via a gitignored `docker-compose.override.yml`). Server-side
  Idempotency-Key dedup makes duplicate POSTs safe. See README §"Second instance / failover".
- **The scaffolder hands out the repo's own artifacts.** `bin/townloops-agent-add <slug>` copies `cron/jobs.json`
  verbatim (no second schedule — per-town divergence goes in the town's `docker-compose.override.yml`) and
  renders `{{SLUG}}`/`{{REPO}}`/`{{CODEX_HOME}}` (repo it runs from, `$CODEX_HOME` else `$HOME/.codex`; a `:` in
  either path is refused) — never a host path in `templates/`; `--rerender` re-applies them and refreshes a
  leader town's `cron/jobs.json` from the repo (a follower's — `jobs.empty.json` bound by the override, or an
  emptied `jobs.json` — is left alone). `tests/test_townloops_agent_add.py` runs the real template and
  asserts everything a scaffolded town schedules exists. `--egress` compiles every town's `allowed_hosts`
  under `TOWNLOOPS_AGENTS_ROOT` into `$TOWNLOOPS_AGENTS_ROOT/_egress/squid.conf` — outside the repo, the file the
  shared egress compose mounts (generated — never hand-edit; written in place) — and prints, but never runs, the
  shared reload (`docker exec squid-egress squid -k reconfigure`). `templates/_shared/egress/squid.conf` is the
  legacy hand-written list, unmounted, kept only for a `squid-egress` created before the move (recreate it once).
- **Firecrawl is always the local instance, never cloud.** Every Firecrawl call resolves its endpoint through
  `engine/firecrawl_env.base_url()` (`TOWNLOOPS_FIRECRAWL_URL` → `FIRECRAWL_API_URL`); unset, or a
  `firecrawl.dev` URL, raises `FirecrawlNotConfigured`. Don't add a cloud default or a second endpoint resolver.
- **Agent judgement lives in `SOUL.md` §Judgement + skill `references/`.** The CMO's hard-won rules
  (business vs venue vs community, recurring series, vague venues/off-town URLs → review, venue images,
  duplicates as operator proposals, source tricks, "say whether live, local or both changed") are
  distilled there. Add new lessons to the matching skill's `references/` and a one-line rule in SOUL.md;
  don't copy town-specific CMO notes wholesale.
- **Agents propose skill changes; they don't patch the repo.** `skills/business/`, `scripts/`, `engine/`,
  `SOUL.md` and `config.yaml` are bind-mounted `:ro` into every town. Per SOUL.md §Improving my own skills,
  a town's agent writes fixes for a business skill to
  `~/townloops-agents/<slug>/hermes-home/skill-proposals/<skill>/` (`PROPOSAL.md` + full changed files
  under their path inside the skill). Review, port into `skills/business/<skill>/`, then delete the
  proposal dir. Town-only skills it creates live in `hermes-home/skills/town-local/` and must not reuse a
  business skill's name (Hermes refuses an ambiguous bare name). `SOUL.md`, `config.yaml` and the
  egress `squid.conf` are single-file mounts: an editor that saves via rename leaves running containers
  on the old copy — write in place (same inode) or recreate the containers.
- **Codex CLI is best-effort.** If the CLI is missing/throttled, LLM (classifier, event-find extraction)
  calls no-op and retry on the next tick — don't treat a single throttle as a bug.
- **Business discovery is in-repo** (`skills/business/business-discovery/`, ported from the CMO's
  archived `townloops-business-discovery`). It only proposes: candidates land in `discovery_candidates`
  (`new` → `verified`/`rejected`, or `already-live` with `live_slug`); sweep/verify never write to TownLoops
  and aren't on cron (Firecrawl budget). Shipping is **operator-gated** (`engine/business_promote.py`): the
  operator's Promote button or a confirmed `promote_businesses` chat widget flips `verified` → `approved`
  (no CLI flag can), and the `business-promote` cron ships only `approved` rows — live re-check, local
  business, enrich, then `engine/entity_ship.ship_entity` (event-ship's entity path) → `promoted`. Operator
  declines are remembered by name. See `docs/business-discovery-handoff.md`. It also covers
  community groups and non-profits (`--kind`; non-profits ship as communities, `kind`
  'nonprofit'), and in autonomous mode `harvest` approves verified candidates itself — only on a
  verdict this verifier produced within `autonomy.verdict_max_age_days`; an imported (`imported_from`) or
  older one is re-verified first (`business_discovery.reverifier`).
  The CMO's stranded candidates and research drafts come in once via `scripts/import_cmo.py`
  (`engine/cmo_import.py`): candidates and business drafts as `verified` candidates
  (`source_signal` `cmo-import:…`), other drafts as `needs-review` entities in `performers.py review`.

## Deployment / routing (VPS)

This product follows the CrimsonCowLabs shared-VPS pattern (see `~/projects/crimsoncowlabs/CLAUDE.md`):
the app container + its deploy live here, but **reverse-proxy/TLS/subdomain routing lives in the master
Caddyfile in `peak-to-crumble-web/`** — edit it there and run `./deploy.sh caddy`, never SSH-edit the
VPS Caddyfile. The dashboard binds loopback (`127.0.0.1:8090`); front it with Caddy + TLS and set
`DASHBOARD_COOKIE_SECURE=true`.

## Agent skills

### Issue tracker

Issues live in GitHub Issues on CrimsonCowLabs/townloops-agent (via `gh`); `.scratch/cmo-port/` is legacy. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/` (created lazily). See `docs/agents/domain.md`.
