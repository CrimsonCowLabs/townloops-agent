#!/usr/bin/env python3
"""entity-live-sync — bring local entity changes (enrichment) to the live
TownLoops rows they belong to. Logic: engine/entity_sync.py.

Then the Series pass (engine/series_sync.py): re-read which TownLoops Series
each shipped event is in, and fill blank Series-shared fields on the upcoming
occurrences this agent shipped from their siblings.

Subcommands:
  run [--limit N] [--kind K ...]   sync entities whose content changed since
                                   live was last verified, then the Series
                                   pass (cron, daily)
  list                             pending + operator-flagged entities (read-only)
  clear --kind K --entity-uid UID  operator: unflag after looking at it
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from engine import entity_sync, series_sync
from engine import lock as _lock
from engine.derive_helpers import open_db


def _run(args) -> int:
    if not all(os.environ.get(k) for k in
               ("TOWNLOOPS_API_URL", "TOWNLOOPS_API_KEY", "TOWNLOOPS_TOWN_SLUG")):
        print("TownLoops env unset — nothing to sync")
        return 0
    from engine.townloops_api import Client
    conn = open_db()
    try:
        # Same lock as the workqueue drain, so event-ship never syncs the
        # same entity at the same time.
        with _lock.acquire("workqueue-drain"):
            client = Client()
            counts = entity_sync.sweep(client, conn, limit=args.limit,
                                       kinds=tuple(args.kind or entity_sync.RESOURCES))
            conn.commit()
            series_counts = series_sync.sweep(client, conn, limit=args.limit)
    except _lock.LockHeld:
        print("workqueue drain running — sync skipped this time")
        return 0
    conn.commit()
    print(json.dumps({**(counts or {"synced": 0}), "series": series_counts}))
    return 0


def _list(args) -> int:
    conn = open_db()
    out = []
    for kind, table in entity_sync.RESOURCES.items():
        for r in conn.execute(f"SELECT * FROM {table} WHERE townloops_slug IS NOT NULL "
                              "AND pushed_at IS NOT NULL ORDER BY entity_uid"):
            r = dict(r)
            flag = entity_sync.live_sync_flag(r)
            if flag or entity_sync.needs_sync(r):
                out.append({"kind": kind, "entity_uid": r["entity_uid"], "name": r["name"],
                            "slug": r["townloops_slug"],
                            "state": f"flagged-{flag['status']}" if flag else "pending",
                            "detail": (flag or {}).get("detail")})
    print(json.dumps(out, indent=2))
    return 0


def _clear(args) -> int:
    conn = open_db()
    ok = entity_sync.clear_flag(conn, args.kind, args.entity_uid)
    conn.commit()
    print("cleared" if ok else "not flagged")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="entity-live-sync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--limit", type=int, default=25)
    run.add_argument("--kind", action="append", choices=sorted(entity_sync.RESOURCES))
    sub.add_parser("list")
    clear = sub.add_parser("clear")
    clear.add_argument("--kind", required=True, choices=sorted(entity_sync.RESOURCES))
    clear.add_argument("--entity-uid", required=True)
    args = ap.parse_args()
    return {"run": _run, "list": _list, "clear": _clear}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
