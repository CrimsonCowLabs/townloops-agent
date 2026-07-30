#!/usr/bin/env python3
"""operator-bulk-approve — PROPOSE a bulk approval. Never mutates.

Read-only sibling of operator-bulk-decline. See that skill's docstring
for the "why Hermes can't execute this" story.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Optional


def _parse_iso_date(s: str) -> str:
    if len(s) == 10:
        datetime.fromisoformat(s)
        return s + "T23:59:59"
    datetime.fromisoformat(s)
    return s


def _select_matches(conn, *, before_cmp: Optional[str],
                    source: Optional[str]) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT event_uid, COALESCE(title, name) AS title, "
        "  COALESCE(date_local, date) AS date_display, "
        "  COALESCE(date_utc, date, first_seen_at) AS date_sort, "
        "  COALESCE(discovered_via, source, '') AS source "
        "FROM events WHERE approval_status = 'pending' "
        "ORDER BY COALESCE(date_utc, date, first_seen_at) ASC"
    ).fetchall()]
    out = []
    src_needle = source.lower() if source else None
    for r in rows:
        if before_cmp:
            key = r.get("date_sort") or ""
            if not key or key >= before_cmp:
                continue
        if src_needle and src_needle not in (r.get("source") or "").lower():
            continue
        out.append(r)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--before", help="ISO date/datetime; events starting BEFORE")
    p.add_argument("--source", help="substring match on discovered_via")
    p.add_argument("--limit", type=int, default=500)
    args = p.parse_args(argv)

    before_cmp: Optional[str] = None
    if args.before:
        try:
            before_cmp = _parse_iso_date(args.before)
        except ValueError as e:
            print(json.dumps({"error": f"bad --before: {e}"}), file=sys.stderr)
            return 2
    source = args.source.strip() if args.source else None
    if not before_cmp and not source:
        print(json.dumps({"error":
            "refusing to preview an unbounded approval; pick at least "
            "--before or --source"}), file=sys.stderr)
        return 2
    if args.limit < 1 or args.limit > 500:
        print(json.dumps({"error": "--limit must be 1..500"}), file=sys.stderr)
        return 2

    from engine import db as _engine_db
    with _engine_db.connect() as c:
        matches = _select_matches(c, before_cmp=before_cmp, source=source)
    matches = matches[:args.limit]

    preview = [
        {"event_uid": r["event_uid"],
         "title":     (r.get("title") or "")[:120],
         "date":      r.get("date_display") or "",
         "source":    (r.get("source") or "")[:100]}
        for r in matches
    ]
    proposal = {
        "kind":    "approve_events",
        "filters": {"before": args.before, "source": source},
        "count":   len(matches),
        "uids":    [r["event_uid"] for r in matches],
    }
    print(json.dumps({
        "proposal": proposal,
        "preview":  preview,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
