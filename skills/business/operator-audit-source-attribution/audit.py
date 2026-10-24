#!/usr/bin/env python3
"""operator-audit-source-attribution — read-only audit of cross-wired live events.

Reports live TownLoops events whose source_url is another event's page: the
page names a different event, or gives a different date (#47). Reads each
source page once through local Firecrawl and judges it with #45's predicate.
Only reads TownLoops (LIST) and the local DB; see
engine/source_attribution_audit.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timezone
from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    from engine.source_attribution_audit import DEFAULT_MAX_FETCHES, audit_source_attribution

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    when = p.add_mutually_exclusive_group()
    when.add_argument("--since", metavar="YYYY-MM-DD",
                      help="only events starting on or after this town-local day")
    when.add_argument("--upcoming", action="store_true",
                      help="only events starting from now on")
    p.add_argument("--max-fetches", type=int, default=DEFAULT_MAX_FETCHES,
                   help="max source pages fetched through Firecrawl (0..5000); the rest are "
                        "listed unverified")
    p.add_argument("--limit", type=int, default=200, help="max rows (1..1000); counts are uncapped")
    args = p.parse_args(argv)

    if not 1 <= args.limit <= 1000:
        print(json.dumps({"error": "limit must be 1..1000"}), file=sys.stderr)
        return 2
    if not 0 <= args.max_fetches <= 5000:
        print(json.dumps({"error": "max-fetches must be 0..5000"}), file=sys.stderr)
        return 2
    since = None
    if args.since:
        try:
            day = date.fromisoformat(args.since)
        except ValueError:
            print(json.dumps({"error": "since must be YYYY-MM-DD"}), file=sys.stderr)
            return 2
        from zoneinfo import ZoneInfo
        from engine.event_extract import town_tz_name
        since = datetime.combine(day, time.min, ZoneInfo(town_tz_name()))
    elif args.upcoming:
        since = datetime.now(timezone.utc)

    from engine import db as _engine_db  # lazy imports for clean --help
    from engine.townloops_api import Client

    client = Client()
    client.verify_town()
    with _engine_db.connect() as c:
        out = audit_source_attribution(c, client, since=since, max_fetches=args.max_fetches,
                                       limit=args.limit)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
