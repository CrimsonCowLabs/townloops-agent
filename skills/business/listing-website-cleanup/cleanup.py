#!/usr/bin/env python3
"""listing-website-cleanup — one-off clear of listing-page websites
(restaurantji, Yelp, maps, ...), then re-enrich. Logic:
engine/listing_website_cleanup.py, on the audit's rows
(engine/listing_website_audit.py). No cron: it runs when the operator asks.

Subcommands:
  list                          every clear, its status and what changed (read-only)
  propose-clear [--uid U ...]   read-only proposal for the chat Confirm widget
                                (supervised); --uid takes targets, uid
                                prefixes or names
  apply [--limit N]             supervised: clear what the OPERATOR confirmed,
                                live first, then local. Autonomous: clear local,
                                flag live ones for TownLoops admin (never a
                                PATCH). Then re-enrich cleared entities.

list/propose-clear never write. `apply` writes only what the operator
confirmed in supervised mode — there is no flag to confirm; confirmation is
the operator's click. Every subcommand first brings the DB up to the repo's
migrations, so the widget's confirm finds the ledger table.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from engine import listing_website_cleanup as lwc
from engine.derive_helpers import open_db, record_activity

_WORKER = "listing-website-cleanup"


def _client():
    from engine.townloops_api import Client
    client = Client()
    client.verify_town()
    return client


def _list(args) -> int:
    print(json.dumps(lwc.ledger(open_db()), indent=2, ensure_ascii=False))
    return 0


def _propose(args) -> int:
    from engine import autonomy
    if autonomy.is_autonomous():
        print(json.dumps(autonomy.inactive_message("listing-website-cleanup propose-clear"),
                         indent=2))
        return 0
    print(json.dumps(lwc.propose(open_db(), _client(), targets=args.uid, limit=args.limit),
                     indent=2, ensure_ascii=False))
    return 0


def _apply(args) -> int:
    from engine import lock as _lock
    if not all(os.environ.get(k) for k in
               ("TOWNLOOPS_API_URL", "TOWNLOOPS_API_KEY", "TOWNLOOPS_TOWN_SLUG")):
        print("TownLoops env unset — nothing to apply")
        return 0
    conn = open_db()
    try:
        # entity-live-sync's lock: never alongside a drain or sync writing the
        # same entity rows.
        with _lock.acquire("workqueue-drain"):
            out = lwc.run(conn, _client(), enrich=lwc.enricher(), limit=args.limit)
    except _lock.LockHeld:
        print("workqueue drain running — try again shortly")
        return 0
    record_activity(conn, _WORKER, "apply", "listing-websites",
                    detail=json.dumps(out, default=str)[:2000])
    conn.commit()
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="listing-website-cleanup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    pp = sub.add_parser("propose-clear")
    pp.add_argument("--uid", action="append", default=None,
                    help="target ('business:<uid>'), uid prefix or name; repeatable. Default: all")
    pp.add_argument("--limit", type=int, default=50)
    apply = sub.add_parser("apply")
    apply.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)
    from engine import db
    db.migrate()  # a code update must not meet an older schema
    return {"list": _list, "propose-clear": _propose, "apply": _apply}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
