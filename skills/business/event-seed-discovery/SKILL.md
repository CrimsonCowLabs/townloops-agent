---
name: event-seed-discovery
description: Propose, verify, and stage event-source URLs for operator approval.
---

# event-seed-discovery

Uses the LLM (Codex CLI) to propose seed source URLs for a town — chamber
of commerce, city government, county government, library, tourism board,
parks & rec, local newspaper, and major venues. These become the starting
points for the daily `event-find` cron.

Runs weekly (Sunday 04:00 UTC) and on first-run from `town-init`. New
proposals land as `event_seeds.status='proposed'` for the operator to
approve, reject, or edit in the dashboard's Seeds panel.

## CLI

```bash
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py discover
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py discover --limit N
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py discover --dry-run
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py show
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py set-role --seed-id N --role syndicator
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py set-adapter --seed-id N --adapter tribe [--config JSON]  # dry-run first; or generic
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py adapters          # shared + town adapters, shadowed, broken
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py test-adapter --adapter NAME --case DIR  # captured response, no network
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py review [--trials N] [--moves N] [--adapters N]
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py probe [--limit N] # seeds never probed
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py probe --all       # re-probe every seed
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py probe --seed-id N
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py health            # seeds with a source-health alert
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py health --ack N    # accept seed N's new event count
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py move --seed-id N --url URL [--note TEXT]
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py hold --seed-id N --reason manual-only|awaiting-adapter [--note TEXT]
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py release --seed-id N
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py restore --seed-id N [--previous-url]
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py retire --seed-id N [--reason TEXT]
```

`discover` runs LLM discovery, HEAD-verifies each URL, and inserts survivors
as proposed seeds, recording each one's feed. It is idempotent and skips URLs already in `event_seeds`.
`show` prints current seed rows as JSON lines.

## Source role (dedup)

When several seeds list the same event, `event-find` keeps one copy
(engine/event_dedup.py) and a **primary** source's copy beats a
**syndicator**'s. A seed's role is its `source_role` when the operator set
one (`set-role`); otherwise known aggregator hosts (allevents, Eventbrite,
Patch, Bandsintown, …) and `newspaper`/`tourism` seeds are syndicators and
everything else is primary. `show` prints the effective `role`. `--role
auto` clears an operator choice.

## Feed probing

Every seed's page is checked for a machine-readable event feed (engine/feed_probe.py) from the
HTML Firecrawl already fetched to verify it — no extra requests. `discover` probes new seeds;
`probe` (daily cron, ≤4 seeds a run to stay inside Hermes' 120s cron script timeout; a no-op
once every seed is probed) catches seeds never probed — untried ones before ones that were
unreachable last time — and `--all` re-probes.
The best find lands in `event_seeds.feed_type` / `feed_url`, everything found in `feed_detail`
(with `resolved_url` when the seed URL is a short link or redirect — feeds resolve against the
target). `show` prints them. Best first:

| feed_type | detected from | feed_url |
|---|---|---|
| `ical` | `<link rel=alternate type=text/calendar>` | the .ics |
| `google-calendar` | `calendar.google.com/calendar/embed?src=…` iframe | `…/calendar/ical/<src>/public/basic.ics` |
| `tribe` | The Events Calendar (WordPress) markup | `/wp-json/tribe/events/v1/events` — paginate by the response's `total_pages`, not `per_page` |
| `showare` | a Showare event widget script (`…/include/widgets/events/*.js`, often protocol-relative) | `https://<box office>/include/widgets/events/performancelist.asp` — page by `perfCount` with `action=perf`, `page`, `listPageSize`, `listMaxSize` |
| `eventcalendarapp` | widget URL with `id` + `widgetUuid` | `api.eventcalendarapp.com/events?id=…&widgetUuid=…` (paginated) |
| `point` | POINT iframe with a `token` | the shared `frames/events?token=…` JSON feed (paginated) |
| `jsonld` | schema.org `Event` (or subtype) JSON-LD on the page | the page |
| `rss` | an RSS/Atom alternate whose title or URL says event/calendar (blog feeds don't count) | the feed |
| `html-calendar` | structured event-list markup | the page |
| `wix-calendar-wrapper` | Wix `filesusr.com/html/` component | none — the Google Calendar src is hidden behind postMessage; don't extract dated events from the widget, find the calendar URL first |

A recorded feed is evidence, not a switch: event-find still scrapes the seed URL until the seed is
given an adapter (below; a `tribe` / `showare` adapter reads the recorded `tribe` / `showare` feed_url —
engine/seed_adapters.py). Basin- or county-wide feeds (POINT orgs, county libraries, destination
calendars) need filtering to the town before any adapter uses them.

## Adapters: set-adapter

Set an adapter when a seed's events come from a feed a registered adapter reads — its probed
`feed_type` is in an adapter's `feed_types` (today `tribe` → `tribe`, `showare` → `showare`), or you've found the
endpoint yourself (source-automation review, event-find SKILL.md) — and especially for a seed held
`awaiting-adapter`. Adapters are the shared ones (`seed_adapters.ADAPTERS`) plus the town's own,
`$HERMES_HOME/adapters/<name>.py` (below); a source none reads yet stays generic or held
`awaiting-adapter` while you write one.

- **`set-adapter --seed-id N --adapter KEY [--config JSON]`** — only a registered key (shared or town); `--config`
  is a JSON object of the settings that adapter declares (its `params` — `tribe`: `url` http(s),
  `max_pages` and `days_ahead` whole numbers above 0; `showare`: `url` http(s), `max_performances`
  a whole number above 0); any other key or a bad value is refused by
  name. A rejected or retired seed is refused (`restore` it first). Then a dry run reads the seed
  through the adapter as event-find would: an `AdapterError`, or no usable events (a title and a
  date — what event-find keeps), refuses, saying why, and the seed is unchanged. On success the
  adapter is saved — decision `seed-adapter` `assign` / `trial-passed` with the usable
  `trial_events`, `from`, `adapter`, `config` (`autonomy why <seed>`) — and printed with
  `trial_events` and `status`. A seed held **`awaiting-adapter` is released** by that good trial,
  in the same transaction (`release`: autonomous → `proposed`, so `review` trials it through the
  adapter; supervised → back to approved if it was). A `manual-only` hold stays held.
- **`set-adapter --seed-id N --adapter generic`** — back to Firecrawl → LLM, no trial; decision
  `revert` / `back-to-generic`. `review` never assigns that seed an adapter again — only an explicit
  `set-adapter` does. On a seed that's already generic it changes and records nothing
  (`"changed": false`).
- **`review`'s adapter step** (first, ≤`--adapters` seeds a run, default 2): a generic seed —
  proposed, approved, or held `awaiting-adapter` (the one held seed review touches, and only through
  this audited assignment + release) — whose `feed_type` maps to an adapter is dry-run with no
  config and judged by `autonomy.judge_adapter`. Autonomous: passed → assigned (reason `feed-match`,
  with `feed_type`/`feed_url`). Supervised: passed → decision `await-operator` / `supervised`,
  listed with its `set-adapter` command under **Adapter proposals** in the nightly digest until the
  operator acts — the agent runs the command on the operator's say-so; once the town is autonomous,
  review assigns it itself. Failed → `keep-generic` / `adapter-trial-failed`, tried again after
  `ADAPTER_RETRY_DAYS` (7) or a new probe; empty → `adapter-trial-empty`, tried again only after a
  new probe (`probe --seed-id N`).

## Town adapters: adapters, test-adapter

A town writes its own adapter as `$HERMES_HOME/adapters/<name>.py` — persistent, this town only —
exposing `ADAPTER = Adapter(read, params, feed_types=(...))` from `engine.seed_adapters` (it may
import `fetch_json`, `html_text`, `local_time`, `location`, `AdapterError`, `Param`). The file name
is the adapter key: a-z, 0-9, `_`, `-` (≤40), never `generic`; `_`-prefixed files are helpers, not
adapters. `read(seed, config, get_json)` returns every event of the listing in the extraction's
shape and fetches only through `get_json` (fixtures stub it; its answers are blocked-page checked).
It is loaded at lookup — `set-adapter`, `review`, event-find — and again when the file changes.

- A shared adapter of the same name wins; the town file is **shadowed** (logged, listed) — so a
  promoted adapter takes over, and the town copy can be deleted.
- A file that fails to import or has no valid `ADAPTER` is skipped, logged and listed with why; a
  seed naming it fails its run with that reason, and no other seed is touched.
- A reader that raises, or returns no list of event objects, is a failed run (`AdapterError`).

```bash
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py adapters
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py test-adapter --adapter NAME --case DIR [--config JSON]
$PYTHON $HERMES_HOME/skills/business/event-seed-discovery/discover.py test-adapter --adapter NAME --fixture PATH [--expected PATH] [--config JSON] [--seed-id N | --seed-url URL]
```

- **`adapters`** — every key a seed can name (`source` shared/town, `feed_types`, `params`), the
  town `shadowed` names and broken files (`errors`, file → why).
- **`test-adapter`** — no network: reads a captured response through the adapter's reader with
  `get_json` answered from the capture, `--config` checked against its `params`. `--fixture` is one
  JSON file (answers every request) or a directory of `page-<N>.json` (answered by the request's
  `page`). `--case DIR` is the layout a promotion carries: `fixture.json` or `fixture/`,
  `expected.json`, optional `seed.json` (`url`/`feed_type`/`feed_url` and a `config`). `--seed-id`
  reads as that seed would. Prints one JSON object — `events`, `requests` and, with expected
  events, `pass` and a unified `diff`; exit 1 on a mismatch or any error. Capture the response
  from inside the container (the adapter's own fetch, or Firecrawl), and keep it for the case.

Then `set-adapter` (its dry run against the live source is the gate), or declare the seed's probed
`feed_type` in `feed_types` and let `review` trial it. Promotion: event-find SKILL.md.

## Source health

event-find alerts the operator when an established seed suddenly lists nothing or sharply fewer
events (engine/source_health.py; details in event-find SKILL.md). `health` lists the seeds with
an alert; `health --ack N` accepts a seed's new level (also the dashboard's **Accept new level**).

## Seed maintenance: move, hold, release

How a seed is fixed — by the agent or the operator. **Never edit `event_seeds` with SQL**, and
no ad-hoc DB backup is needed: every change below is reversible and recorded as a decision in
`agent_activity`, so `autonomy why <seed id or name>` explains it (verdict, reason, evidence).

- **`move --seed-id N --url URL [--note …]`** — correct a seed's URL. The new URL is read first
  the way event-find will read it: an adapter seed (`set-adapter`) through its adapter at the new
  URL (an `AdapterError` refuses), any other seed's page through Firecrawl — a failed fetch or a
  challenge/error/near-empty page (engine/blocked_page) refuses, and nothing changes. On success
  the old URL is kept as `previous_url`, the health baseline starts again, the status is kept;
  decision `move` / `url-corrected` with `from`, `to`, what was `checked`, the note. A URL another
  seed already reads is refused.
- **`restore --seed-id N --previous-url`** — undo a `move` (or a review's calendar relocation):
  the URLs swap back, the status is kept unless the seed was retired/rejected (then approved).
  Decision `restore` with `from`/`to`. Without `--previous-url`, `restore` only brings back a
  retired or rejected seed.
- **`hold --seed-id N --reason manual-only|awaiting-adapter [--note …]`** — status `held`
  (`hold_reason`; the note is kept in the hold decision). event-find never scrapes a held seed and the
  autonomous `review` never trials, re-approves, re-judges, relocates or retires it (its adapter
  step may assign an `awaiting-adapter` seed its adapter, which releases it — see Adapters). Use it for a
  seed that only a person can read (`manual-only`) or that waits for a deterministic adapter
  (`awaiting-adapter` — a passing `set-adapter` trial releases it, and review's adapter step may
  assign one) — not `rejected` with a note. Held seeds and their reasons are listed in
  the nightly digest and the dashboard's Seeds band (`/seeds held` in chat).
- **`release --seed-id N`** — back to service. Supervised: `approved` if it was approved when held,
  else `proposed` for the operator — never straight to approved. Autonomous: `proposed`, so the
  next `review` trials it before event-find reads it again. The decision records `held_from`/`to`.

## Prompt design

Given the town name, aliases, and region, the prompt asks Codex CLI for a
compact JSON array of seed candidates with `name`, `url`, and `kind`.
Kinds are constrained to chamber, government, tourism, library, parks,
newspaper, venue, or other.

## URL verification

Every LLM-proposed URL is HEAD-checked via Firecrawl. A 2xx response is required for initial staging, but it is not evidence that the URL is a usable automated source. Branded 404 pages, challenge pages, empty JavaScript widgets, and vendor error responses can all return 2xx.

Before moving a `manual_only` seed to automated operation, run the source-automation review in `event-find`: establish a dry-run baseline on a copied database, inspect raw and rendered content, identify stable JSON/iCal/vendor endpoints, verify pagination and geographic scope, and classify error pages as failed runs rather than successful empty runs. Prefer replacing a broken seed URL with the current canonical calendar URL instead of teaching extraction around a branded error page.

## Auto-approve gate

Nothing is auto-approved. The operator reviews proposals in the dashboard.
This is intentional: a bad seed can create substantial review noise.

## Autonomous mode: `review` (daily cron `event-seed-review`)

In supervised mode only the adapter step runs, and only proposes (see Adapters). In autonomous mode:

- **Adapters** first: a seed whose probed feed a registered adapter reads is dry-run on it and
  assigned when the trial returns events (see Adapters) — a held `awaiting-adapter` seed is
  released into this run's proposed trials.
- **Proposed seeds** (feeds first, `--trials` per run, default 2) are dry-run:
  scrape, extract, and judge the listed events as event-find would (an
  adapter seed through its adapter). The
  trial's score — the smoothed share of listed events that are real upcoming
  in-town events, `(passed + 1) / (judged + 2)`, stored as its starting
  `trust_score` — must reach `seed_trust.approve` (default 0.2) with at least
  one passing; short of it is `trial-low-yield`. An error page
  (`trial-not-found`), bot wall (`trial-challenge`), empty page or empty
  listing, or nothing usable is a rejection. A 2xx alone never approves.
- **Trust** (`trust_score`, 0..1) is refreshed from moderator outcomes since
  the seed's last status change (an approved or restored seed starts at
  0.5), scaled down by a health alert and by dedup losses (not counted for a
  syndicator). It drives the two calls below, each only once
  `seed_retirement.min_outcomes` (default 5) submissions are decided; every
  decision carries the score in its evidence.
- **Role**: trust below `seed_trust.syndicator` (default 0.45) → demoted to
  `syndicator` (`low-trust`). Otherwise a seed whose events mostly link to
  other sites is a `syndicator`; one whose events link to its own site is
  `primary` (`source_role`, so dedup keeps the organiser's copy).
- **Retire**: trust below `seed_trust.retire` (default 0.3) → `retired`
  (`low-trust`); under a standing health alert it is re-probed for a moved
  calendar first, as below. Its last
  `dead_runs` runs all failed or listed nothing, or a health alert persisted
  that long → first look for a moved calendar (where the seed page redirects
  now, then a same-site search), dry-running each candidate; one that passes
  **replaces the URL** (`previous_url` kept), otherwise the seed is retired.
- Retirement is a status change, never a delete: `restore --seed-id N`
  (`--previous-url` to undo a move) brings it back; `retire --seed-id N`
  retires one by hand.
- **Held** seeds (`hold`) are skipped entirely until `release` — except an
  `awaiting-adapter` one the adapter step assigns, which releases it.
