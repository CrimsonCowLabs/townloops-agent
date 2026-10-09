"""Read-only audit of the town's next N days on TownLoops.

Three findings, each a list capped at `limit` plus an uncapped count:

- **gaps** — town-local days in the window with no live event, and local
  events dated in the window that aren't live yet (pending review, still in
  the pipeline, or stuck). The local side is the "found by a source but not on
  TownLoops" half of the CMO's next-30-days gap audit, without re-scraping.
- **coverage** — per town-local day, the distinct live groups (a Series
  counts once, however many occurrences it has that day), and the days whose
  every group is a Series: where one-off coverage is thin.
- **sparse_metadata** — live events missing a description, venue binding,
  source_url or categories, one finding per group: a Series missing a
  description on 44 occurrences is one finding.
- **venues_missing_images** — venues of window events with no `image_path`,
  busiest first. Events carry no image field; the listing shows the venue's.

Grouping is `engine.event_groups` — on `series_slug`, never on title.

The window is whole town-local days. TownLoops filters `from`/`to` on UTC
instants, so the fetch runs a day past the local end and every `start_at` is
re-bucketed by its local date — otherwise late-evening events on the last day
fall off.

Only LIST/GET calls; the operator decides what to fix.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from engine.derive_helpers import dict_rows as _rows, parse_instant, utc_z
from engine.event_groups import group_events
from engine.event_extract import town_tz_name


# Field → "is it missing?", in report order.
SPARSE_CHECKS: dict[str, Callable[[dict], bool]] = {
    "description": lambda r: not (r.get("description") or "").strip(),
    "venue":       lambda r: not (r.get("venue_id") or r.get("venue_slug")),
    "source_url":  lambda r: not (r.get("source_url") or "").strip(),
    "categories":  lambda r: not (r.get("category_slugs") or r.get("category_slug")),
}


def audit_upcoming(conn: sqlite3.Connection, client, *, days: int = 30,
                   now: Optional[datetime] = None, limit: int = 50) -> dict:
    tz_name = town_tz_name()
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(timezone.utc)
    first = now.astimezone(tz).date()
    last = first + timedelta(days=days - 1)
    window_start = datetime.combine(first, time.min, tz)
    window_end = datetime.combine(last + timedelta(days=1), time.min, tz)   # exclusive

    live = []
    for row in client.paginate("events", page_size=100, **{"from": utc_z(window_start),
                                                            "to": utc_z(window_end + timedelta(days=1))}):
        start = parse_instant(row.get("start_at"))
        if start and window_start <= start < window_end:
            live.append((start.astimezone(tz).date(), row))
    live.sort(key=lambda p: (p[0], p[1].get("start_at") or "", p[1].get("slug") or ""))
    day_of = {id(row): day for day, row in live}
    groups = group_events([row for _, row in live])

    return {
        "window": {"from": first.isoformat(), "to": last.isoformat(), "days": days, "tz": tz_name},
        "live_events": len(live),
        "live_groups": len(groups),
        "gaps": {
            "empty_days": _empty_days(first, last, {d for d, _ in live}),
            "not_live": _not_live(conn, window_start, window_end, limit),
        },
        "coverage": _coverage(first, last, groups, day_of),
        "sparse_metadata": _sparse(groups, day_of, limit),
        "venues_missing_images": _venues_missing_images(client, groups, day_of, limit),
    }


def _empty_days(first: date, last: date, busy: set[date]) -> list[str]:
    out, d = [], first
    while d <= last:
        if d not in busy:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _coverage(first: date, last: date, groups: list, day_of: dict) -> dict:
    per_day: dict[date, dict[str, bool]] = {}
    for g in groups:
        for row in g.events:
            per_day.setdefault(day_of[id(row)], {})[g.key] = g.is_series
    days, series_only, d = [], [], first
    while d <= last:
        seen = per_day.get(d, {})
        series = sum(seen.values())
        days.append({"date": d.isoformat(), "groups": len(seen), "series": series,
                     "singletons": len(seen) - series})
        if seen and series == len(seen):
            series_only.append(d.isoformat())
        d += timedelta(days=1)
    return {"days": days, "series_only_days": series_only}


def _not_live(conn, lo: datetime, hi: datetime, limit: int) -> dict:
    # String prefilter a day early (date_utc formats vary), exact cut in Python.
    rows = _rows(conn, """
        SELECT event_uid, COALESCE(title, name) AS title, date_utc, date_local,
               state, approval_status, COALESCE(discovered_via, source) AS source
          FROM events
         WHERE state != 'shipped' AND approval_status != 'declined'
           AND townloops_slug IS NULL AND COALESCE(hidden, 0) = 0
           AND date_utc >= ?
    """, ((lo - timedelta(days=1)).date().isoformat(),))
    counts = {"pending_review": 0, "in_pipeline": 0, "stuck": 0}
    found = []
    for r in rows:
        start = parse_instant(r["date_utc"])
        if not start or not lo <= start < hi:
            continue
        if (r["state"] or "").startswith("stuck-"):
            reason = "stuck"
        elif r["approval_status"] == "pending":
            reason = "pending_review"
        else:
            reason = "in_pipeline"
        counts[reason] += 1
        found.append((start, r["event_uid"], {
            "event_uid": r["event_uid"], "title": (r["title"] or "")[:200],
            "date": r["date_local"] or r["date_utc"], "state": r["state"],
            "source": r["source"] or "", "reason": reason,
        }))
    found.sort(key=lambda f: (f[0], f[1]))
    return {"counts": counts, "total": len(found), "events": [f[2] for f in found[:limit]]}


def _sparse(groups: list, day_of: dict, limit: int) -> dict:
    counts = dict.fromkeys(SPARSE_CHECKS, 0)
    events = []
    for g in groups:
        hit = [(row, [f for f, is_missing in SPARSE_CHECKS.items() if is_missing(row)])
               for row in g.events]
        hit = [(row, missing) for row, missing in hit if missing]
        if not hit:
            continue
        missing = [f for f in SPARSE_CHECKS if any(f in m for _, m in hit)]
        for f in missing:
            counts[f] += 1
        row = hit[0][0]
        events.append({"slug": row.get("slug"), "title": (row.get("title") or "")[:200],
                       "date": day_of[id(row)].isoformat(), "source_url": row.get("source_url"),
                       "missing": missing, "series_slug": g.series_slug, "occurrences": len(hit)})
    return {"counts": counts, "total": len(events), "events": events[:limit]}


def _venues_missing_images(client, groups: list, day_of: dict, limit: int) -> dict:
    by_venue: dict[str, dict] = {}
    for g in groups:
        for row in g.events:
            slug = row.get("venue_slug")
            if slug:
                v = by_venue.setdefault(slug, {"events_in_window": 0, "groups": set(),
                                               "next_event": day_of[id(row)]})
                v["events_in_window"] += 1
                v["groups"].add(g.key)
                v["next_event"] = min(v["next_event"], day_of[id(row)])
    if not by_venue:
        return {"total": 0, "venues": [], "unverified": []}

    venues = {v.get("slug"): v for v in client.paginate("venues")}
    missing, unverified = [], []
    for slug, seen in by_venue.items():
        venue = venues.get(slug)
        if venue is None:
            unverified.append(slug)
        elif not (venue.get("image_path") or "").strip():
            missing.append({"venue_slug": slug, "name": venue.get("name"),
                            "website": venue.get("website"),
                            "events_in_window": seen["events_in_window"],
                            "groups_in_window": len(seen["groups"]),
                            "next_event": seen["next_event"].isoformat()})
    missing.sort(key=lambda v: (-v["groups_in_window"], -v["events_in_window"],
                                v["next_event"], v["venue_slug"]))
    return {"total": len(missing), "venues": missing[:limit], "unverified": sorted(unverified)}
