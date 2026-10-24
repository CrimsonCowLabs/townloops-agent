---
name: operator-audit-source-attribution
description: Read-only audit of live TownLoops events whose source_url is another event's page — the page names a different event or gives a different date (cross-wired listing cards).
---

# operator-audit-source-attribution

## When to use

The operator asks whether live events carry the wrong source page: "audit
source attribution", "which live events point at another event's page?",
"find cross-wired events", or wants the scope of the rows #45's guard now
stops but which shipped before it (SLT's `line-dancing-2026-10-08`: a dance
class on a half marathon's detail URL, date and time).

Read-only. It only LISTs TownLoops events, reads the local DB and fetches
source pages through local Firecrawl; it proposes nothing and changes
nothing, live or local. The judgement is `engine/event_provenance.page_disagreements`
— the check read-detail holds new events on. A better check goes there, not
here.

## CLI

```bash
python skills/business/operator-audit-source-attribution/audit.py                     # every live event
python skills/business/operator-audit-source-attribution/audit.py --upcoming
python skills/business/operator-audit-source-attribution/audit.py --since 2026-10-01 --limit 50
python skills/business/operator-audit-source-attribution/audit.py --max-fetches 2000
```

- `--since YYYY-MM-DD` / `--upcoming`: only events starting on or after that
  town-local day / from now. Default: every live event, past ones included —
  a cross-wired past row is still a wrong public page.
- `--max-fetches`: source pages fetched, 0–5000, default 300. Each distinct
  page is fetched once; past the cap its events are `unverified`. Firecrawl
  is budgeted (≈1 scrape/s), so a whole-town sweep takes a while — run it
  from a shell, not a chat turn.
- `--limit`: max rows, 1–1000, default 200. Counts and `total` are never capped.

## Output

```json
{
  "since": null, "live_events": 1174, "checked": 702, "pages_fetched": 655, "pages_cached": 3,
  "skipped": {"no_source_url": 41, "listing_page": 431},
  "total": 1,
  "rows": [{"slug": "line-dancing-2026-10-08", "title": "Line Dancing",
            "start_at": "2026-10-09T13:30:00Z", "date": "2026-10-09",
            "venue": "Cascade Kitchens", "status": "published",
            "source_url": "https://business.tahoechamber.org/events/details/lakeside-marathon-nevada-half-marathon-10-09-2026-17355",
            "disagreed": ["detail page does not name \"Line Dancing\" (it reads \"Lakeside Marathon - Nevada Half Marathon\")"],
            "found_by": ["detail-page"], "event_uid": "…", "seed": "Tahoe Chamber"}],
  "unverified": [{"source_url": "…", "slugs": ["…"], "error": "cloudflare challenge"}],
  "local_flags": [{"event_uid": "…", "title": "…", "date": "…", "townloops_slug": "…",
                   "source_url": "…", "reason": "…"}]
}
```

- One row per live event, by start. `title`, `date` (town-local) and
  `venue` are **live's** — live is what's wrong; `event_uid` and `seed` are
  the local row paired by `townloops_slug` (null when none).
- `disagreed` — one sentence per finding:
  - `detail page does not name "<title>" (it reads "<heading>")` — the page
    is another event's. The strongest signal.
  - `detail page does not give <date> (it gives …)` — the page is about
    this event but states other dates: another occurrence's page, or a
    moved date. Occurrences of one title sharing a page (a Series' page)
    are never held to its one date.
  - a listing reason (`found_by: "listing"`) — event-find saw on the seed's
    listing that this link is another card's (#45's `live-mismatch` flag,
    also in the nightly digest), while live still carries that link.
- There is no venue check: pages often name no venue, or name it another
  way. A cross-wired row is caught by its title or date.
- `skipped` — not fetched: no `source_url`, or the seed's listing page (a
  listing names many events; it is no one event's page). `listing_page`
  rows are no evidence either way.
- `unverified` — the page couldn't be read (Firecrawl error, a challenge
  or error page, the fetch cap): **unknown**, not clean. Re-run later or
  with a higher `--max-fetches`.
- `local_flags` — local `live-mismatch` flags whose slug no longer lists
  live (deleted, merged or renamed) — check TownLoops admin by hand.

## Reading the result

- Report counts first: live events, how many were checked, how many
  disagreed, how many are unverified.
- Each row is a lead for a human, not a fix. Open its `source_url` and the
  seed's listing, then decide in TownLoops admin: correct the title, date
  and source_url, or delete it — several cross-wired rows turned out to be
  deletions. This audit never changes a row; don't PATCH from it by hand.
- A `does not give` row whose page is a recurring event's general page may
  be fine — say so rather than call it cross-wired.
