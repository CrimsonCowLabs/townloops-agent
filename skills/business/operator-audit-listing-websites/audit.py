#!/usr/bin/env python3
"""operator-audit-listing-websites — read-only audit of listing-page websites.

Lists every venue, business and community whose `website` is a directory or
listing page (restaurantji, Yelp, maps, ticketing, ...), locally, live or
both. Only reads TownLoops (LIST/GET) and the local DB; see
engine/listing_website_audit.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--limit", type=int, default=200, help="max rows (1..1000); counts are uncapped")
    args = p.parse_args(argv)

    if not 1 <= args.limit <= 1000:
        print(json.dumps({"error": "limit must be 1..1000"}), file=sys.stderr)
        return 2

    from engine import db as _engine_db  # lazy imports for clean --help
    from engine.listing_website_audit import audit_listing_websites
    from engine.townloops_api import Client

    client = Client()
    client.verify_town()
    with _engine_db.connect() as c:
        out = audit_listing_websites(c, client, limit=args.limit)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
