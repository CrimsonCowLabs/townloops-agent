---
name: event-find
description: Discover upcoming events from approved event seeds, validate source automation, and stage normalized events for operator review.
---

# event-find

Daily pass that scrapes every `approved` event seed and uses the LLM (Codex CLI) to extract upcoming events. Events land at `state=metadata-extracted`, `approval_status=pending` so the operator can approve or decline them in the dashboard.

The current implementation is a generic Firecrawl + LLM path. It is useful for simple pages but is not proof that every approved source is safely automatable. Known calendar platforms and large or recurring sources should use deterministic adapters when implemented.

## Why one skill does find+extract in v1

The specification has separate `event-find` and `event-extract-metadata` stages. In v1, the find-time LLM prompt already returns title, date, venue, and description, so events skip the separate metadata pass and enter `metadata-extracted`. A future per-event extraction stage can add images, RSVP URLs, prices, or detail-page metadata without changing source discovery.

## CLI

```bash
find.py                       # scrape every approved seed
find.py --seed-id N           # scrape one seed by id
find.py --limit N             # cap seeds this run
find.py --dry-run             # scrape + extract, print without insert
```

## Current generic per-seed loop

1. Firecrawl-scrape the seed URL as markdown.
2. Ask the LLM for a JSON array of upcoming events for the configured town.
3. Upsert each record with `event_uid = sha1(seed_name + normalized_title + start_iso)`.
4. Set `state=metadata-extracted`, `approval_status=pending`, and `discovered_via=seed:<name>`.
5. Link each new row to its `canonical_event_uid` (engine/event_dedup.py): copies of one event
   from other seeds — same local day, matching title, no conflicting venue or start time — share
   it, and only one copy stays pending. The others are `status=duplicate`, declined with a
   `[dedup]` note. A primary source's copy beats a syndicator's (`event-seed-discovery set-role`),
   and a copy the operator already decided or that is live is never displaced.
6. Count the run against the seed's upcoming rows for cancellation detection
   (engine/event_cancel.py — see the `event-cancellations` skill).
7. Record the run (events listed, inserted, updated) in `source_runs` and judge the seed's
   health (engine/source_health.py — see below).
8. Update `event_seeds.last_scraped_at` and `last_event_count`.

After all seeds: rows from before dedup get canonical uids (start of run), and live events
missing from all their sources for `cancellation_miss_threshold` finds are flagged
`possibly-cancelled` for the operator.

## Source health

An established seed (`min_runs`+ successful runs) whose latest successful run lists 0 events
(it averaged ≥1), or under half its trailing average (it averaged `min_baseline`+), gets
`event_seeds.health_status` `zero`/`drop` and one Telegram alert plus a `source-health` activity
row; the dashboard Seeds table shows the badge. Failed runs (Firecrawl/Codex errors, or Codex
output that isn't a JSON array — an explicit `[]` is a real zero) are never judged. The alert holds — no repeats — until a run is back to half the old average
(`recovered`, also sent), or the operator accepts the new level (dashboard **Accept new level**,
or `event-seed-discovery/discover.py health --ack SEED_ID`); the seed is then judged again only once
`min_runs` new runs make up its new baseline. A dry run isn't recorded. Tune via
`source_health:` in town-profile.yaml.

When a seed goes quiet, check the page before blaming the calendar: a redesign, a moved
calendar URL, or a widget that now loads from a vendor feed (re-run
`event-seed-discovery probe --seed-id N` to see what the page exposes now).

## Reviewing manual-only sources for automation

Do not approve a manual source merely because it returns HTTP 200 or produces one plausible LLM result.

1. Use a copied town database for `--dry-run`; do not alter production seed status during the audit.
2. Record a generic-path baseline per seed, including zero-result sources and likely overcounts.
3. Inspect rendered content, raw HTML, links, iframes, JSON-LD, and calendar-vendor scripts.
4. Probe structured vendor APIs or iCal endpoints before designing an HTML scraper. Start from
   the seed's recorded `feed_type`/`feed_url` (`event-seed-discovery show`).
5. Validate pagination, timezone behavior, recurrence representation, stable source IDs, and town or branch affinity.
6. Prefer deterministic adapters in this order: JSON/iCal, stable HTML cards, specialized recurrence or annual parser, then LLM fallback.
7. Normalize adapter records into `engine.event_extract.upsert_event()` so event state, deduplication, pending approval, and workqueue behavior remain unchanged.
8. Re-run all reviewed seeds against the copied database and compare counts and deduplication with the generic baseline before changing production configuration.

If adapters are implemented, select them through per-seed configuration such as an adapter key. Do not scatter hostname or URL conditionals through the extraction loop.

## Failure and quality checks

- Treat branded 404s, Cloudflare or Incapsula challenges, empty JS widgets, and vendor error text as failed source runs, not successful zero-event runs.
- Handle every pagination mechanism explicitly. Never infer full coverage from the first API response or rendered page.
- Avoid passing entire large calendars through a fixed LLM content window; truncation silently drops events.
- For annual landing pages, constrain extraction to the primary event section or canonical ticket listing. Social and news dates often become false events.
- Expand recurrence text only when the rule is explicit, within a bounded rolling horizon.
- For multi-region sources, apply reliable source-level branch or geography filters where available.
- Automation changes extraction, not approval. New events remain pending unless an explicit product policy says otherwise.

## Idempotency and cost

Rerunning a seed deduplicates on `event_uid`; already approved events are not clobbered. The generic path costs one Firecrawl call and one LLM call per seed. Deterministic adapters can remove the LLM call but may require pagination and detail-page requests.

## References

- `references/manual-source-automation-review.md` — reusable audit checklist, adapter-selection hierarchy, failure semantics, and the 2026 South Lake Tahoe manual-seed findings.
- `references/source-tricks.md` — Wix-wrapped Google Calendar, Tribe paging, EventCalendarApp, POINT, regional-feed filtering, short links, Facebook login walls.
- `../operator-audit-upcoming/references/upcoming-audit-playbook.md` §Off-town or redirecting source_url, §Null or vague venues — extracted events whose source resolves out of town, or whose venue is a fragment, go to operator review.
