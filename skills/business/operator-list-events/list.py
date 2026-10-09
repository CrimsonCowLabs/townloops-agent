#!/usr/bin/env python3
"""operator-list-events — filtered event listing for the operator chat.

Invoked by Hermes. Read-only, runs SQL directly against the state DB via
engine.db.connect (the dashboard package that owns db_v2 isn't mounted
in the agent container).

A Series collapses to one row with its date count (engine.event_groups —
on `series_slug`, never on title); `--series <slug>` expands one into every
live occurrence through TownLoops' `?series=` filter.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Optional


_STATUSES = ("pending", "approved", "declined", "shipped")

_SELECT_COLS = """
  event_uid,
  COALESCE(title, name)                                    AS title,
  COALESCE(date_local, date)                               AS date_display,
  COALESCE(date_utc,   date, first_seen_at)                AS date_sort,
  COALESCE(venue_name, venue_raw)                          AS venue,
  COALESCE(location,   location_raw)                       AS location,
  description,
  category_slug,
  series_slug,
  COALESCE(discovered_via, source, '')                     AS source
"""


def _parse_iso_date(s: str) -> str:
    """Accept 'YYYY-MM-DD' or full ISO. Return ISO for string compare
    against events.date_utc/date. Raises ValueError on bad input so the
    caller can surface a validation message."""
    if len(s) == 10:
        datetime.fromisoformat(s)
        return s + "T23:59:59"
    datetime.fromisoformat(s)
    return s


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--status", default="pending", choices=_STATUSES)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--before", help="ISO date/datetime; events starting BEFORE this")
    p.add_argument("--source", help="substring match on discovered_via (case-insensitive)")
    p.add_argument("--series", help="series_slug: list every live occurrence of that Series")
    args = p.parse_args(argv)

    if args.series:
        return _expand_series(args.series)

    if args.limit < 1 or args.limit > 100:
        print(json.dumps({"error": "limit must be 1..100"}), file=sys.stderr)
        return 2

    before_cmp: Optional[str] = None
    if args.before:
        try:
            before_cmp = _parse_iso_date(args.before)
        except ValueError as e:
            print(json.dumps({"error": f"bad --before: {e}"}), file=sys.stderr)
            return 2

    from engine import db as _engine_db  # lazy import for clean --help

    with _engine_db.connect() as c:
        if args.status == "shipped":
            rows = [dict(r) for r in c.execute(
                f"SELECT {_SELECT_COLS} FROM events WHERE state='shipped' "
                f"ORDER BY state_updated_at DESC"
            ).fetchall()]
        else:
            rows = [dict(r) for r in c.execute(
                f"SELECT {_SELECT_COLS} FROM events WHERE approval_status = ? "
                f"ORDER BY COALESCE(date_utc, date, first_seen_at) ASC",
                (args.status,),
            ).fetchall()]

    src_needle = args.source.lower() if args.source else None
    matched: list[dict] = []
    for r in rows:
        if before_cmp:
            date_key = r.get("date_sort") or ""
            if not date_key or date_key >= before_cmp:
                continue
        if src_needle:
            if src_needle not in (r.get("source") or "").lower():
                continue
        matched.append(r)

    from engine.event_groups import group_local_rows

    # Groups keep the query's order (oldest-first, or most recently shipped).
    order = {r["event_uid"]: i for i, r in enumerate(matched)}
    groups = sorted(group_local_rows(matched), key=lambda g: min(order[r["event_uid"]] for r in g.events))
    filtered: list[dict] = []
    for g in groups[:args.limit]:
        r = g.representative
        filtered.append({
            "event_uid":   r["event_uid"],
            "title":       (r.get("title") or "")[:200],
            "date":        r.get("date_display") or "",
            "venue":       (r.get("venue") or "")[:200],
            "location":    (r.get("location") or "")[:200],
            "source":      (r.get("source") or "")[:120],
            "category":    r.get("category_slug") or "",
            "description": (r.get("description") or "")[:400],
            "series_slug": g.series_slug,
            "dates":       len(g.events),
        })

    payload = {
        "status": args.status,
        "count":  len(filtered),
        "events_total": sum(e["dates"] for e in filtered),
        "filters": {
            "before": args.before or None,
            "source": args.source or None,
            "limit":  args.limit,
        },
        "events": filtered,
    }
    print(json.dumps(payload, indent=2))
    return 0


def _expand_series(slug: str) -> int:
    """Every occurrence of one Series on TownLoops (past included, each with
    its `status`), oldest first. Read-only."""
    from engine.event_groups import SeriesDetails
    from engine.townloops_api import Client

    client = Client()
    series = SeriesDetails(client).get(slug) or {}
    events = sorted(client.paginate("events", series=slug),
                    key=lambda e: (e.get("start_at") or "", e.get("slug") or ""))
    payload = {
        "series": {"slug": slug, "title": series.get("title"),
                   "cadence_text": series.get("cadence_text"), "is_active": series.get("is_active")},
        "count": len(events),
        "events": [{"slug": e.get("slug"), "title": (e.get("title") or "")[:200],
                    "start_at": e.get("start_at"), "venue_slug": e.get("venue_slug"),
                    "status": e.get("status")} for e in events],
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
