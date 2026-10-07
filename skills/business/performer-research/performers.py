#!/usr/bin/env python3
"""performer-research — research performers/creators along the evidence
ladder in small batches and fill blank fields only from verified, sourced
profiles. Logic: engine/performer_research.py.

Subcommands:
  run [--limit N] [--lookups N]       research the next batch (cron, every 20 min)
  review                              entities waiting for an operator identity call
  resolve --kind K --entity-uid UID (--accept URL | --reject)
                                      operator: attach a recorded candidate, or none
"""

from __future__ import annotations

import argparse
import json
import sys

from engine import lock as _lock
from engine import performer_research as pr
from engine.derive_helpers import open_db, record_activity, town_name
from engine.research import FirecrawlAuthError

_WORKER = "performer-research"


def _run(args) -> int:
    conn = open_db()
    try:
        with _lock.acquire(_WORKER):
            results = pr.run_batch(conn, town_name(), limit=args.limit, lookups=args.lookups)
    except _lock.LockHeld:
        print("previous performer-research run still active — skipped")
        return 0
    except FirecrawlAuthError as e:
        record_activity(conn, _WORKER, "researched", "batch", ok=False, error=str(e)[:200])
        print(str(e), file=sys.stderr)
        return 1
    for r in results:
        review = r["status"] in pr.REVIEW_FINDINGS
        detail = f"{r['kind']} {r['name']}: {r['status']}"
        if r["filled"]:
            detail += f" ({','.join(r['filled'])})"
        record_activity(conn, _WORKER, "needs-review" if review else "researched",
                        r["entity_uid"], detail=f"{detail} — {r['why']}", ok=not review,
                        error=r["why"][:200] if review else None)
    print(json.dumps({"researched": len(results), "results": results}, ensure_ascii=False))
    return 0


def _review(args) -> int:
    print(json.dumps(pr.review_queue(open_db()), indent=2, ensure_ascii=False))
    return 0


def _resolve(args) -> int:
    conn = open_db()
    try:
        filled = pr.resolve(conn, args.kind, args.entity_uid,
                            accept_url=None if args.reject else args.accept)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    action = "operator-rejected" if args.reject else "operator-verified"
    record_activity(conn, _WORKER, action, args.entity_uid, detail=",".join(filled))
    print(json.dumps({"status": action, "filled": filled}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="performer-research")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--limit", type=int, default=3)
    run.add_argument("--lookups", type=int, default=pr.DEFAULT_LOOKUPS,
                     help="Firecrawl searches + page reads per entity")
    sub.add_parser("review")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--kind", required=True, choices=pr.REVIEW_KINDS)
    resolve.add_argument("--entity-uid", required=True)
    choice = resolve.add_mutually_exclusive_group(required=True)
    choice.add_argument("--accept", metavar="URL", help="a candidate URL from `review`")
    choice.add_argument("--reject", action="store_true", help="none of the candidates")
    args = ap.parse_args()
    return {"run": _run, "review": _review, "resolve": _resolve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
