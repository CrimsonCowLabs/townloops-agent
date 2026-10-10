#!/usr/bin/env python3
"""event-cancellations — live events that vanished from their sources.
Logic: engine/event_cancel.py (detection runs inside event-find).

Subcommands:
  list                             events flagged possibly-cancelled (or whose
                                   live marking failed), soonest first
  propose-confirm [--uid U ...]    read-only proposal to confirm cancellations
                                   (dashboard chat renders it as a Confirm widget)
  propose-dismiss [--uid U ...]    read-only proposal to dismiss flags (still on)
  apply [--limit N]                mark OPERATOR-confirmed cancellations on
                                   TownLoops: "Cancelled: " title + `cancelled` tag

list/propose-* never write. `apply` writes only what the operator confirmed
— there is no flag to confirm; confirmation is the operator's click.

In autonomous mode `apply` instead re-reads flagged events' own pages and
submits a pending cancellation only where the page says it is cancelled or
postponed (engine/event_cancel.submit_evidenced); the rest stay flagged for
the nightly digest.
"""

from __future__ import annotations

import argparse
import json
import sys

from engine import event_cancel as ec
from engine import lock as _lock
from engine.derive_helpers import open_db, record_activity

_WORKER = "event-cancellations"


def _list(args) -> int:
    print(json.dumps([ec.preview(r) for r in ec.flagged(open_db())], indent=2,
                     ensure_ascii=False))
    return 0


def _propose(kind):
    def run(args) -> int:
        from engine import autonomy
        if autonomy.is_autonomous():
            print(json.dumps(autonomy.inactive_message(f"event-cancellations propose-{kind}"),
                             indent=2))
            return 0
        print(json.dumps(ec.propose(open_db(), kind, uids=args.uid, limit=args.limit),
                         indent=2, ensure_ascii=False))
        return 0
    return run


def _apply_autonomous(conn, args) -> int:
    from engine import autonomy
    from engine.event_extract import firecrawl_markdown
    from engine.townloops_api import Client
    if not conn.execute("SELECT 1 FROM events WHERE cancel_status = 'possibly-cancelled' "
                        "LIMIT 1").fetchone():
        print(json.dumps({"checked": 0}))
        return 0
    try:
        with _lock.acquire(_WORKER):
            client = Client()
            client.verify_town()
            out = ec.submit_evidenced(conn, autonomy.GatedClient(client, conn, autonomy.settings()),
                                      firecrawl_markdown, limit=min(args.limit, 3))
    except _lock.LockHeld:
        print("previous event-cancellations run still active — skipped")
        return 0
    if out["checked"]:
        record_activity(conn, _WORKER, "evidence-check", "cancellations",
                        detail=json.dumps({k: v for k, v in out.items() if k != "errors"}),
                        ok=not out["errors"], error="; ".join(out["errors"])[:1000] or None)
    print(json.dumps(out, indent=2))
    return 0


def _apply(args) -> int:
    conn = open_db()
    from engine import autonomy
    if autonomy.is_autonomous():
        return _apply_autonomous(conn, args)
    if not conn.execute("SELECT 1 FROM events WHERE cancel_status = 'confirmed' LIMIT 1").fetchone():
        # The cron's usual case: no API call, no activity row.
        print(json.dumps({"cancelled": 0}))
        return 0
    from engine.townloops_api import Client
    try:
        with _lock.acquire(_WORKER):
            client = Client()
            client.verify_town()
            out = ec.apply_confirmed(conn, client, limit=args.limit)
    except _lock.LockHeld:
        print("previous event-cancellations run still active — skipped")
        return 0
    record_activity(conn, _WORKER, "apply", "cancellations",
                    detail=json.dumps({k: v for k, v in out.items() if k != "errors"}),
                    ok=not out["failed"], error="; ".join(out["errors"])[:1000] or None)
    print(json.dumps(out, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="event-cancellations")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("propose-confirm", "propose-dismiss"):
        pp = sub.add_parser(name)
        pp.add_argument("--uid", action="append", default=None,
                        help="event uid (or prefix); repeatable. Default: all flagged")
        pp.add_argument("--limit", type=int, default=50)
    apply = sub.add_parser("apply")
    apply.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)
    return {"list": _list, "propose-confirm": _propose("confirm"),
            "propose-dismiss": _propose("dismiss"), "apply": _apply}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
