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
python skills/business/event-seed-discovery/discover.py probe [--limit N] # seeds never probed
python skills/business/event-seed-discovery/discover.py probe --all       # re-probe every seed
python skills/business/event-seed-discovery/discover.py probe --seed-id N
python skills/business/event-seed-discovery/discover.py health            # seeds with a source-health alert
python skills/business/event-seed-discovery/discover.py health --ack N    # accept seed N's new event count
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
a switch: event-find still scrapes the seed URL. Basin- or county-wide feeds (POINT orgs, county
libraries, destination calendars) need filtering to the town before any adapter uses them.

## Source health

event-find alerts the operator when an established seed suddenly lists nothing or sharply fewer
events (engine/source_health.py; details in event-find SKILL.md). `health` lists the seeds with
an alert; `health --ack N` accepts a seed's new level (also the dashboard's **Accept new level**).

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
  scrape, extract, and judge the listed events as event-find would. Approved
  only when at least one is a real upcoming in-town event; an error page
  (`trial-not-found`), bot wall (`trial-challenge`), empty page or empty
  listing, or nothing usable is a rejection. A 2xx alone never approves.
- **Role**: a seed whose events mostly link to other sites is a
  `syndicator`; one whose events link to its own site is `primary`
  (`source_role`, so dedup keeps the organiser's copy).
- **Trust** (`trust_score`) is refreshed from moderator outcomes, health and
  dedup losses.
- **Retire**: most of its submissions rejected by the moderator
  (`seed_retirement.reject_rate` over `min_outcomes`) → `retired`. Its last
  `dead_runs` runs all failed or listed nothing, or a health alert persisted
  that long → first look for a moved calendar (where the seed page redirects
  now, then a same-site search), dry-running each candidate; one that passes
  **replaces the URL** (`previous_url` kept), otherwise the seed is retired.
- Retirement is a status change, never a delete: `restore --seed-id N`
  (`--previous-url` to undo a move) brings it back; `retire --seed-id N`
  retires one by hand.
