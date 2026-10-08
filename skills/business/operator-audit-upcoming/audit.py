#!/usr/bin/env python3
"""operator-audit-upcoming — read-only audit of the town's next N days.

Reports days with no live event, local events not yet live, live events with
sparse metadata, and venues of upcoming events that have no image. Only
reads TownLoops (LIST/GET) and the local DB; see engine/upcoming_audit.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=30, help="window length in town-local days (1..90)")
    p.add_argument("--limit", type=int, default=50, help="max rows per list (1..200); counts are uncapped")
    args = p.parse_args(argv)

    if not 1 <= args.days <= 90:
        print(json.dumps({"error": "days must be 1..90"}), file=sys.stderr)
        return 2
    if not 1 <= args.limit <= 200:
        print(json.dumps({"error": "limit must be 1..200"}), file=sys.stderr)
        return 2

    from engine import db as _engine_db  # lazy imports for clean --help
    from engine.townloops_api import Client
    from engine.upcoming_audit import audit_upcoming

    client = Client()
    client.verify_town()
    with _engine_db.connect() as c:
        out = audit_upcoming(c, client, days=args.days, limit=args.limit)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
