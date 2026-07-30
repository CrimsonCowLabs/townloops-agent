#!/usr/bin/env python3
"""operator-review-event — fuzzy single-event lookup for chat.

Matches on `title LIKE ?`, `venue_name LIKE ?`, `venue_raw LIKE ?`, and
`event_uid LIKE prefix%`. Ranked by whichever match kind hit + how many
non-null enrichment fields the row has (more-enriched preferred as the
disambiguation exemplar)."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional


_ROW_COLS = """
  event_uid,
  COALESCE(title, name)                       AS title,
  COALESCE(date_local, date)                  AS date_display,
  COALESCE(date_utc,   date, first_seen_at)   AS date_sort,
  COALESCE(venue_name, venue_raw)             AS venue,
  COALESCE(location,   location_raw)          AS location,
  description,
  category_slug,
  COALESCE(discovered_via, source, '')        AS source,
  approval_status,
  state,
  approval_note,
  approval_at
"""


def _match_rank(row: dict, needle: str) -> int:
    """Rank matches: exact title match > title-startswith > title-contains
    > venue > uid-prefix. Ties broken by whether venue/description exist."""
    needle_low = needle.lower()
    title_low = (row.get("title") or "").lower()
    venue_low = (row.get("venue") or "").lower()
    uid_low   = row["event_uid"].lower()

    if title_low == needle_low:
        base = 0
    elif title_low.startswith(needle_low):
        base = 1
    elif needle_low in title_low:
        base = 2
    elif needle_low in venue_low:
        base = 3
    elif uid_low.startswith(needle_low):
        base = 4
    else:
        base = 5
    enrichment_penalty = 0
    if not row.get("venue"): enrichment_penalty += 1
    if not row.get("description"): enrichment_penalty += 1
    return base * 10 + enrichment_penalty


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args(argv)

    q = args.query.strip()
    if not q or len(q) > 200:
        print(json.dumps({"error": "--query must be 1..200 chars"}),
              file=sys.stderr)
        return 2
    limit = max(1, min(20, args.limit))

    from engine import db as _engine_db
    like = f"%{q}%"
    uid_prefix = f"{q}%"

    with _engine_db.connect() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT {_ROW_COLS} FROM events "
            "WHERE title LIKE ? OR name LIKE ? "
            "   OR venue_name LIKE ? OR venue_raw LIKE ? "
            "   OR event_uid LIKE ? "
            "ORDER BY COALESCE(date_utc, date, first_seen_at) ASC "
            "LIMIT 100",
            (like, like, like, like, uid_prefix),
        ).fetchall()]

    if not rows:
        print(json.dumps({"query": q, "match_count": 0, "matches": []}))
        return 0

    rows.sort(key=lambda r: _match_rank(r, q))
    top = rows[:limit]

    def _shape(r: dict, *, full: bool) -> dict:
        base = {
            "event_uid":       r["event_uid"],
            "title":           r.get("title") or "",
            "date":            r.get("date_display") or "",
            "venue":           r.get("venue") or "",
            "source":          r.get("source") or "",
            "approval_status": r.get("approval_status") or "",
            "state":           r.get("state") or "",
        }
        if full:
            base.update({
                "location":     r.get("location") or "",
                "description":  (r.get("description") or "")[:800],
                "category":     r.get("category_slug") or "",
                "approval_note": r.get("approval_note") or "",
                "approval_at":  r.get("approval_at") or "",
            })
        return base

    is_single = len(top) == 1
    payload = {
        "query":       q,
        "match_count": len(top),
        "total_hits":  len(rows),
        "matches":     [_shape(r, full=is_single) for r in top],
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
