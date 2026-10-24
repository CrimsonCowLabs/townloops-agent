"""Read-only audit of live events whose source_url is another event's page
(#47) — the cross-wired rows #45 now stops, but which shipped before it
(SLT's `line-dancing-2026-10-08`: a dance class on a half marathon's detail
URL, date and time).

Each live event's own source page is read and judged by #45's predicate,
`event_provenance.page_disagreements` — the check read-detail holds on, so
the two can't drift: a heading must name the event's live title, and its
section must give its town-local date (unless another live occurrence of
the same title links the same page — a Series' page). Live is what's
wrong, so the live title and start are judged, never the local row's.
There is no venue check: a page that names no venue, or names it another
way ("Cascade Kitchens" / "Cascade Kitchens Food Hall & Bar", an address),
is the common case, not a cross-wire.

- Each distinct source page is fetched once, through local Firecrawl
  (`event_extract.firecrawl_listing`: a challenge or error page is a failed
  read), at most `max_fetches` times — Firecrawl is budgeted. A page
  read-detail cached for the paired local row is used instead of a fetch.
- Not fetched: an event with no source_url, or whose source_url is a seed's
  listing page (a listing names many events; it is no event's own page).
- A failed, blocked or capped read leaves its events in `unverified`, never
  in the report: unread is not a mismatch.
- Live rows pair with local rows by `townloops_slug` (event_uid and seed,
  for context). A local row #45 flagged `live-mismatch` (its listing shows
  the link is another card's) adds its reason to the live row while live
  still carries that link; a flag whose slug no longer lists live is
  returned in `local_flags`.

Only LIST calls to TownLoops and SELECTs; it repairs nothing. Deciding what
a cross-wired row should say needs a human reading the source listing.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable, NamedTuple, Optional

from engine import blocked_page, event_detail
from engine.derive_helpers import dict_rows as _rows, parse_instant, utc_z
from engine.event_extract import day_and_clock, firecrawl_listing
from engine.event_provenance import canon_url, live_mismatches, page_disagreements


DEFAULT_MAX_FETCHES = 300


class CrossWired(NamedTuple):
    rows: list[dict]
    unverified: list[dict]    # {source_url, slugs, error}: its page unread
    local_flags: list[dict]   # live-mismatch flags whose slug no longer lists live
    skipped: dict             # {no_source_url, listing_page}: not fetched
    live_events: int          # listed (since `since`)
    checked: int              # events judged against their page
    pages_fetched: int
    pages_cached: int


def audit_source_attribution(conn: sqlite3.Connection, client, *, since: Optional[datetime] = None,
                             max_fetches: int = DEFAULT_MAX_FETCHES, limit: int = 200,
                             fetch: Optional[Callable] = None) -> dict:
    """find_cross_wired, summarised: uncapped counts, the first `limit` rows,
    and every unverified page and stray flag."""
    found = find_cross_wired(conn, client, since=since, max_fetches=max_fetches, fetch=fetch)
    return {"since": utc_z(since) if since else None, "live_events": found.live_events,
            "checked": found.checked, "pages_fetched": found.pages_fetched,
            "pages_cached": found.pages_cached, "skipped": found.skipped,
            "total": len(found.rows), "rows": found.rows[:limit],
            "unverified": found.unverified, "local_flags": found.local_flags}


def find_cross_wired(conn: sqlite3.Connection, client, *, since: Optional[datetime] = None,
                     max_fetches: int = DEFAULT_MAX_FETCHES,
                     fetch: Optional[Callable] = None) -> CrossWired:
    """Every live event (starting at or after `since`, if given) its source
    page or its listing says isn't its own, ordered by start. Each row:

        {"slug", "title", "start_at", "date" (town-local), "venue", "status",
         "source_url", "disagreed" (one sentence each),
         "found_by": ["detail-page" | "listing"], "event_uid", "seed"
         (the paired local row's; None when unpaired)}
    """
    fetch = fetch or (lambda u: firecrawl_listing(u, timeout_s=event_detail.FETCH_TIMEOUT_S))
    params = {"from": utc_z(since)} if since else {}
    live = [r for r in client.paginate("events", page_size=100, **params)
            if not since or (parse_instant(r.get("start_at")) or since) >= since]
    live.sort(key=lambda r: (r.get("start_at") or "", r.get("slug") or ""))
    local = _local_rows(conn)
    seeds = {u for u in (canon_url(r["url"]) for r in _rows(conn, "SELECT url FROM event_seeds"))
             if u}

    # Who links each page: a page two live occurrences of one title share is
    # a Series' page, which gives one date.
    titles: dict[str, list[str]] = {}
    skipped = {"no_source_url": 0, "listing_page": 0}
    by_url: dict[str, list[dict]] = {}
    for r in live:
        url = canon_url(r.get("source_url"))
        if url is None:
            skipped["no_source_url"] += 1
        elif url in seeds:
            skipped["listing_page"] += 1
        else:
            by_url.setdefault(url, []).append(r)
            titles.setdefault(url, []).append((r.get("title") or "").strip().lower())

    disagreed: dict[str, list[str]] = {}
    found_by: dict[str, list[str]] = {}
    unverified, fetched, cached, checked = [], 0, 0, 0
    for url, events in by_url.items():
        page = _cached(local, events, url)
        if page is not None:
            cached += 1
        elif fetched >= max_fetches:
            unverified.append(_unread(url, events, f"not fetched: fetch cap ({max_fetches}) reached"))
            continue
        else:
            fetched += 1
            try:
                ok, page, err = fetch(events[0]["source_url"])
            except TimeoutError as e:   # the Firecrawl token bucket stayed empty
                ok, page, err = False, "", str(e)
            blocked = blocked_page.reason(page, None, None) if ok else None
            if not ok or blocked:
                unverified.append(_unread(url, events, err or blocked))
                continue
        for r in events:
            checked += 1
            title = (r.get("title") or "").strip().lower()
            reasons = page_disagreements({"title": r.get("title"), "date": r.get("start_at")}, page,
                                         shared_page=titles[url].count(title) > 1)
            if reasons:
                disagreed[r["slug"]] = list(reasons)
                found_by[r["slug"]] = ["detail-page"]

    by_slug = {r.get("slug"): r for r in live}
    local_flags = []
    for flag in live_mismatches(conn, upcoming=False):
        r = by_slug.get(flag["townloops_slug"])
        if r is None:
            if not since:   # a since-sweep doesn't list the rest
                local_flags.append(flag)
            continue
        if canon_url(r.get("source_url")) != canon_url(flag["source_url"]):
            continue   # live no longer carries the link
        disagreed.setdefault(r["slug"], []).append(flag["reason"])
        found_by.setdefault(r["slug"], []).append("listing")

    rows = [_finding(r, disagreed[r["slug"]], found_by[r["slug"]], local.get(r["slug"]))
            for r in live if r.get("slug") in disagreed]
    return CrossWired(rows, unverified, local_flags, skipped, len(live), checked, fetched, cached)


def _local_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    """Local rows by their live slug, a shipped one first."""
    out: dict[str, dict] = {}
    for r in _rows(conn, "SELECT event_uid, townloops_slug, COALESCE(discovered_via, source) AS seed, "
                         "detail_read_status, detail_read_url FROM events "
                         "WHERE townloops_slug IS NOT NULL "
                         "ORDER BY state = 'shipped' DESC, event_uid"):
        out.setdefault(r["townloops_slug"], r)
    return out


def _cached(local: dict, events: list[dict], url: str) -> Optional[str]:
    """The page read-detail kept for one of these events, if it read `url`."""
    for r in events:
        row = local.get(r.get("slug"))
        if row and row["detail_read_status"] == "read" and canon_url(row["detail_read_url"]) == url:
            page = event_detail.cached_page(row["event_uid"])
            if page:
                return page
    return None


def _unread(url: str, events: list[dict], error: str) -> dict:
    return {"source_url": events[0].get("source_url") or url,
            "slugs": [r.get("slug") for r in events], "error": (error or "")[:200]}


def _finding(live: dict, disagreed: list[str], found_by: list[str],
             local: Optional[dict]) -> dict:
    return {
        "slug": live.get("slug"),
        "title": live.get("title"),
        "start_at": live.get("start_at"),
        "date": (day_and_clock(live.get("start_at")) or (None,))[0],
        "venue": live.get("venue_name") or live.get("venue_slug"),
        "status": live.get("status"),
        "source_url": live.get("source_url"),
        "disagreed": disagreed,
        "found_by": found_by,
        "event_uid": local["event_uid"] if local else None,
        "seed": local["seed"] if local else None,
    }
