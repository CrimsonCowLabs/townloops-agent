# Source hunting (run order step 3)

Sources come from evidence, never from memory alone. `S=$HERMES_HOME/skills/business`.

## The verbs

- `event-seed-discovery hunt --max-firecrawl N --max-proposals N` — the
  daily cron already runs one at 04:15; in the analyst run I run it again
  only with budget left, or with `--inputs domains,sites` after I added
  entities with websites. Inputs: `domains` (hosts events link to),
  `sites` (known organisations' calendars), `search` (town calendar
  queries; needs Firecrawl search working), `memory` (weekly at most).
- `event-seed-discovery review --trials N` — trial proposed seeds the same
  day (autonomous). The trial is the gate: a seed with no usable upcoming
  events is rejected with its reason (`autonomy why seed:<name>`).
- `event-seed-discovery show` — every seed and its status, feed, adapter.
- `event-seed-discovery probe --seed-id N` — re-detect a seed's feed after
  the site changed.
- `event-seed-discovery health` — seeds that dropped to zero or sharply
  down; `--ack N` accepts a new normal once I've checked it's real.
- `event-seed-discovery move --seed-id N --url URL` — the calendar moved;
  `hold --seed-id N --reason manual-only|awaiting-adapter`, `release`,
  `retire --seed-id N --reason …`, `restore --seed-id N`.

## What I look for

- A known organisation (venue, business, community) whose own site has a
  calendar page and no seed: its site is the primary source. Find the page
  (homepage nav, `/events`, `/calendar`), then let `hunt --inputs sites`
  propose it, or insert it as `proposed` myself (SQL, `discovered_by`
  `'analyst'`, evidence in `notes`, logged with `local-data log`) and trial it.
- Domains shipped events link to that aren't a seed (`hunt --inputs domains`).
- A seed whose listing is thin but whose feed is rich: probe it, then an
  adapter (below). An 800-event iCal is a good source, not a low-yield one.

## Never

- Re-propose a host that has a seed in any status — a rejected or retired
  source is remembered; `restore` it only on new evidence, noted.
- Aggregators, ticketing and listing hosts (engine/listing_hosts), social
  platforms, articles and "things to do" roundups as seeds.
- Open a wildcard or another town's host with `egress-hosts add`.

## Adapters

`event-seed-discovery adapters` lists shared + my own. If a seed's probed
feed matches one: `event-seed-discovery set-adapter --seed-id N --adapter
NAME [--config JSON]` (saved only after a dry run returns events). If none
reads it: write `$HERMES_HOME/adapters/<name>.py` (SOUL.md §Improving my own
skills), capture a real response, `event-seed-discovery test-adapter
--adapter NAME --fixture FILE --expected FILE`, then `set-adapter`. Shape and
contract: `event-find/SKILL.md` and `event-find/references/source-tricks.md`.

## Notebook

A source I couldn't settle (bot wall, egress pending, needs an adapter) gets
one notebook line: URL, what's blocking, when to retry.
