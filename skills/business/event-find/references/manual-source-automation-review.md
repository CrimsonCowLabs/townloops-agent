# Manual source automation review

Use this reference when converting `manual_only` event seeds into reliable automated sources.

## Audit method

1. Read the seed rows and preserve production state; use a copied SQLite database for discovery dry-runs.
2. Establish a generic-path baseline: events found per source, empty sources, duplicate/overcount behavior, and malformed dates.
3. Inspect both rendered content and raw HTML. Record redirects, challenge/error pages, links, iframes, JSON-LD, and embedded calendar vendors.
4. Probe stable first-party or vendor endpoints before designing a scraper. Prefer JSON/iCal over HTML cards, and HTML cards over LLM extraction.
5. Validate pagination, date-window parameters, recurrence representation, source identity, timezone, and geographic/branch scope.
6. Send normalized records through the existing `upsert_event()` path so deduplication, pending approval, and workqueue behavior remain intact.
7. Re-run every reviewed seed against the copied database and compare counts/deduplication with the generic baseline before changing production seed configuration.

## Adapter selection hierarchy

1. Structured JSON/iCal API.
2. Stable server-rendered event cards plus detail pages.
3. Specialized annual-event or recurrence parser.
4. Pruned rendered content with LLM extraction as fallback only.
5. Keep manual when access is challenge-protected or no stable source can be proven.

Adapters should be selected through per-seed configuration (for example an adapter key), not scattered URL conditionals. They must paginate explicitly and use stable vendor IDs plus occurrence start time where available.

A town writes a new one as `$HERMES_HOME/adapters/<name>.py`, captures the source's response from inside the container, checks it with `event-seed-discovery test-adapter`, sets it with `set-adapter` (dry-run gate), and proposes a proven one under `skill-proposals/engine-adapters/<name>/` (event-find SKILL.md, Promoting a town adapter).

## Failure handling

Treat HTTP error pages, branded 404s, Cloudflare/Incapsula challenges, empty widgets, and vendor messages such as `no action` as failed source runs, not successful zero-event runs. A 2xx response alone does not prove that a source is usable.

For annual landing pages, constrain parsing to the primary event section or canonical ticket listing; social/news dates frequently become false events. For recurrence text, expand only explicit rules into a bounded rolling horizon and retain operator approval.

## South Lake Tahoe review — 2026-10-06

Fourteen imported manual seeds (IDs 11–24) were audited without changing production. The generic Firecrawl + 12,000-character LLM path found 42 events across eight sources and zero across six, demonstrating truncation and source-specific extraction failures.

Validated mechanisms:

- Tahoe Chamber: replace the broken seed URL with `https://business.tahoechamber.org/events/`; parse GrowthZone cards and detail pages.
- El Dorado County Library: EngagedPatrons with `SiteID=7839` and SLT `BranchID=1000000563`; preserve concrete occurrences.
- The Shops at Heavenly: WordPress Tribe Events REST API; pagination is mandatory (15 pages/733 rows observed).
- LTCC: Modern Campus public calendar API, calendar `820f2290-4714-43f1-a00f-c8373f784878`; use repeated `category[]` parameters.
- Valhalla: Showare `performancelist.asp` with `action=perf`, page, `listPageSize`, and `listMaxSize`; identify occurrences by `EventID` + `PerformanceID` — now the `showare` adapter (`set-adapter --adapter showare`).
- Tahoe Blue, Sierra State Parks, and Bally's: deterministic event-card/detail-page adapters.
- Blue Granite, Lake Tahoe Marathon, Tahoe Brewfest, and Sample the Sierra: specialized recurrence or single-annual-event adapters.
- Harrah's: intermittent Incapsula blocking; remain manual until a stable Caesars/Ticketmaster path is proven.
- Camp Richardson Oktoberfest: duplicate of the broader Camp Richardson source; reject/archive after verifying parent-source coverage.

Town-specific full report: `~/.hermes/profiles/slt/state/reports/2026-10-06-manual-seed-automation.md`.
