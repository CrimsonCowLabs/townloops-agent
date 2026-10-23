#!/usr/bin/env python3
"""operator-bulk-approve — PROPOSE a bulk approval. Never mutates.

Read-only sibling of operator-bulk-decline. See that skill's docstring
for the "why Hermes can't execute this" story.

Events held for review (engine/review_hold: a source mismatch, an
implausible time) are never in a bulk approval — approving one accepts what
it was held for, so each is approved on its own, its reason in view. They
are listed under `skipped_held`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Optional

from engine import review_hold


def _parse_iso_date(s: str) -> str:
    if len(s) == 10:
        datetime.fromisoformat(s)
        return s + "T23:59:59"
    datetime.fromisoformat(s)
    return s


SKIPPED_WHY = ("held for review — approving one accepts what it was held for, so approve "
               "them one at a time with the reason in view: the dashboard, or "
               "`autonomy accept <uid>`")


def _select_matches(conn, *, before_cmp: Optional[str],
                    source: Optional[str]) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT event_uid, COALESCE(title, name) AS title, "
        "  COALESCE(date_local, date) AS date_display, "
        "  COALESCE(date_utc, date, first_seen_at) AS date_sort, "
        "  COALESCE(discovered_via, source, '') AS source, "
        f"  NOT {review_hold.NOT_HELD_SQL} AS held, extra_json "
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
    from engine import autonomy
    if autonomy.is_autonomous():
        print(json.dumps(autonomy.inactive_message("operator-bulk-approve"), indent=2))
        return 0

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
    held = [r for r in matches if r["held"]]
    matches = [r for r in matches if not r["held"]][:args.limit]

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
    out = {"proposal": proposal, "preview": preview}
    if held:
        out["skipped_held"] = {
            "count":  len(held),
            "why":    SKIPPED_WHY,
            "events": [{"event_uid": r["event_uid"],
                        "title":     (r.get("title") or "")[:120],
                        "date":      r.get("date_display") or "",
                        "held_for":  [k.label for k in review_hold.KINDS
                                      if review_hold.mark_of(r["extra_json"], k).get("status")
                                      == review_hold.HELD_FOR_REVIEW]}
                       for r in held],
        }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
