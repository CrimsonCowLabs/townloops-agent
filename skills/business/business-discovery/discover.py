#!/usr/bin/env python3
"""business-discovery — find local businesses that aren't on TownLoops yet.
Logic: engine/business_discovery.py (store + live dedup) on the
engine/discovery.py kernel (Firecrawl sweeps + page verify).

Subcommands:
  sweep [--per-source N]           web search + seed directories (+ Facebook) from
                                   town-profile.yaml → discovery.business; new
                                   candidates land as `new`, live matches as `already-live`
  verify [--limit N]               verify `new` candidates' pages → verified / rejected
  list [--status S|all] [--limit N]
                                   candidates (default: the proposed ones — verified, new)
  stats                            candidate counts by status

Never writes to TownLoops.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from engine import business_discovery as bd
from engine import lock as _lock
from engine.derive_helpers import load_town_profile, open_db, record_activity

_WORKER = "business-discovery"


def _town_slug() -> str:
    return os.environ.get("TOWNLOOPS_TOWN_SLUG", "")


def _client():
    from engine.townloops_api import Client
    client = Client()
    client.verify_town()
    return client


def _locked_run(action: str, fn) -> int:
    conn = open_db()
    try:
        with _lock.acquire(_WORKER):
            out = fn(conn, _client(), load_town_profile(), _town_slug())
    except _lock.LockHeld:
        print("previous business-discovery run still active — skipped")
        return 0
    summary = {k: v for k, v in out.items() if k != "errors"}
    record_activity(conn, _WORKER, action, "batch", detail=json.dumps(summary),
                    ok=not out["errors"], error="; ".join(out["errors"])[:200] or None)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _sweep(args) -> int:
    return _locked_run("swept", lambda conn, client, profile, slug: bd.run_sweep(
        conn, client, profile, slug, per_source=args.per_source))


def _verify(args) -> int:
    return _locked_run("verified", lambda conn, client, profile, slug: bd.run_verify(
        conn, client, profile, slug, limit=args.limit))


def _list(args) -> int:
    rows = bd.list_candidates(open_db(), _town_slug(), status=args.status, limit=args.limit)
    keep = ("candidate_uid", "status", "display_name", "canonical_url", "source_signal",
            "live_slug", "reject_reason", "first_seen_at")
    out = []
    for r in rows:
        item = {k: r[k] for k in keep}
        v = r["verify"] or {}
        item.update({k: v[k] for k in ("address", "phone", "hours") if v.get(k)})
        out.append(item)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _stats(args) -> int:
    print(json.dumps(bd.stats(open_db(), _town_slug()), indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="business-discovery")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sweep = sub.add_parser("sweep")
    sweep.add_argument("--per-source", type=int, default=25)
    verify = sub.add_parser("verify")
    verify.add_argument("--limit", type=int, default=20)
    ls = sub.add_parser("list")
    ls.add_argument("--status", default=None,
                    help="new | verified | rejected | already-live | all (default: new + verified)")
    ls.add_argument("--limit", type=int, default=50)
    sub.add_parser("stats")
    args = ap.parse_args()
    if not _town_slug():
        print("TOWNLOOPS_TOWN_SLUG not set", file=sys.stderr)
        return 1
    return {"sweep": _sweep, "verify": _verify, "list": _list, "stats": _stats}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
