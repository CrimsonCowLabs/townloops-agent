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
python skills/business/event-seed-discovery/discover.py discover
python skills/business/event-seed-discovery/discover.py discover --limit N
python skills/business/event-seed-discovery/discover.py discover --dry-run
python skills/business/event-seed-discovery/discover.py show
python skills/business/event-seed-discovery/discover.py set-role --seed-id N --role syndicator
python skills/business/event-seed-discovery/discover.py set-adapter --seed-id N --adapter tribe [--config JSON]  # or generic
python skills/business/event-seed-discovery/discover.py probe [--limit N] # seeds never probed
python skills/business/event-seed-discovery/discover.py probe --all       # re-probe every seed
python skills/business/event-seed-discovery/discover.py probe --seed-id N
python skills/business/event-seed-discovery/discover.py health            # seeds with a source-health alert
python skills/business/event-seed-discovery/discover.py health --ack N    # accept seed N's new event count
python skills/business/event-seed-discovery/discover.py move --seed-id N --url URL [--note TEXT]
python skills/business/event-seed-discovery/discover.py hold --seed-id N --reason manual-only|awaiting-adapter [--note TEXT]
python skills/business/event-seed-discovery/discover.py release --seed-id N
python skills/business/event-seed-discovery/discover.py restore --seed-id N [--previous-url]
python skills/business/event-seed-discovery/discover.py retire --seed-id N [--reason TEXT]
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
| `eventcalendarapp` | widget URL with `id` + `widgetUuid` | `api.eventcalendarapp.com/events?id=…&widgetUuid=…` (paginated) |
| `point` | POINT iframe with a `token` | the shared `frames/events?token=…` JSON feed (paginated) |
| `jsonld` | schema.org `Event` (or subtype) JSON-LD on the page | the page |
| `rss` | an RSS/Atom alternate whose title or URL says event/calendar (blog feeds don't count) | the feed |
| `html-calendar` | structured event-list markup | the page |
| `wix-calendar-wrapper` | Wix `filesusr.com/html/` component | none — the Google Calendar src is hidden behind postMessage; don't extract dated events from the widget, find the calendar URL first |

A recorded feed is evidence for the manual source-automation review (event-find SKILL.md), not
a switch: event-find still scrapes the seed URL until the seed is given an adapter (`set-adapter`;
a `tribe` adapter reads the recorded `tribe` feed_url — engine/seed_adapters.py). Basin- or county-wide feeds (POINT orgs, county
libraries, destination calendars) need filtering to the town before any adapter uses them.

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
  autonomous `review` never trials, re-approves, re-judges, relocates or retires it. Use it for a
  seed that only a person can read (`manual-only`) or that waits for a deterministic adapter
  (`awaiting-adapter`) — not `rejected` with a note. Held seeds and their reasons are listed in
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

Inactive in supervised mode. In autonomous mode:

- **Proposed seeds** (feeds first, `--trials` per run, default 2) are dry-run:
  scrape, extract, and judge the listed events as event-find would. The
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
- **Held** seeds (`hold`) are skipped entirely until `release`.
