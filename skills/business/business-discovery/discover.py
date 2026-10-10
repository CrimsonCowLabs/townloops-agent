#!/usr/bin/env python3
"""business-discovery — find local businesses, community groups and non-profits
that aren't on TownLoops yet (`--kind business|community|nonprofit`, default business).
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
  propose-promote [--uid U ...]    read-only proposal to promote verified candidates
                                   (dashboard chat renders it as a Confirm widget)
  propose-decline [--uid U ...]    read-only proposal to decline candidates for good
  promote [--limit N]              ship the candidates the OPERATOR approved: live
                                   check → local business → enrich → ship
  harvest [--limit N]              autonomous mode only: sweep + verify every kind the
                                   town profile configures, and approve the verified
                                   ones itself (engine/autonomy.judge_candidate)

sweep/verify/list/propose-* never write to TownLoops. `promote` writes only
what the operator approved (status `approved`) — there is no flag to
approve; approval is the operator's click (engine/business_promote.py). In
autonomous mode the agent's own `harvest` approves verified candidates, and
`promote` submits through the gated client: every entity lands `pending`
in the TownLoops moderation queue.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from engine import business_discovery as bd
from engine import business_promote as bp
from engine import lock as _lock
from engine.derive_helpers import load_town_profile, open_db, record_activity

_WORKER = "business-discovery"


def _town_slug() -> str:
    return os.environ.get("TOWNLOOPS_TOWN_SLUG", "")


def _client(conn=None):
    from engine import autonomy
    from engine.townloops_api import Client
    client = Client()
    client.verify_town()
    return autonomy.gated(client, conn) if conn is not None else client


def _locked_run(action: str, fn) -> int:
    conn = open_db()
    try:
        with _lock.acquire(_WORKER):
            out = fn(conn, _client(conn), load_town_profile(), _town_slug())
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
        conn, client, profile, slug, per_source=args.per_source, kind=args.kind))


def _verify(args) -> int:
    return _locked_run("verified", lambda conn, client, profile, slug: bd.run_verify(
        conn, client, profile, slug, limit=args.limit, kind=args.kind))


def _harvest(args) -> int:
    from engine import autonomy
    if not autonomy.is_autonomous():
        print(json.dumps({**autonomy.inactive_message("business-discovery harvest"),
                          "message": "harvest runs in autonomous mode only — in supervised "
                                     "mode sweeps are run by hand and the operator approves"}))
        return 0

    def run(conn, client, profile, slug):
        out: dict = {"errors": []}
        for kind in bd.configured_kinds(profile):
            swept = bd.run_sweep(conn, client, profile, slug, per_source=args.per_source,
                                 kind=kind)
            verified = bd.run_verify(conn, client, profile, slug, limit=args.limit, kind=kind)
            approved = bp.auto_approve(conn, slug, kind)
            out[kind] = {"swept": {k: v for k, v in swept.items() if k != "errors"},
                         "verified": {k: v for k, v in verified.items() if k != "errors"},
                         "approved": approved}
            out["errors"] += swept["errors"] + verified["errors"]
        return out
    return _locked_run("harvested", run)


def _list(args) -> int:
    rows = bd.list_candidates(open_db(), _town_slug(), status=args.status, limit=args.limit,
                              kind=args.kind)
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


def _propose(kind):
    def run(args) -> int:
        from engine import autonomy
        if autonomy.is_autonomous():
            print(json.dumps(autonomy.inactive_message(f"business-discovery propose-{kind}"),
                             indent=2))
            return 0
        print(json.dumps(bp.propose(open_db(), _town_slug(), kind, uids=args.uid,
                                    limit=args.limit), indent=2, ensure_ascii=False))
        return 0
    return run


# Lookup and classifier budgets for a promote run, as event-enrich-entities.
_DEFAULT_MAX_LOOKUPS = 8
_MAX_CLASSIFY_CALLS = 2
_CLASSIFY_TIMEOUT_S = 30
_CLASSIFY_DEADLINE_S = 100


def _promote(args) -> int:
    from engine import entity_classify, entity_enrich
    from engine.derive_helpers import town_name
    kinds_ = list(bd.KINDS) if args.kind == "all" else [args.kind]
    if not any(bd.list_candidates(open_db(), _town_slug(), status="approved", limit=1, kind=k)
               for k in kinds_):
        # The 15-min cron's usual case: no API call, no activity row.
        print(json.dumps({"processed": 0}))
        return 0
    started = time.monotonic()
    try:
        max_lookups = int(os.environ.get("TOWNLOOPS_ENRICH_MAX_LOOKUPS", _DEFAULT_MAX_LOOKUPS))
    except ValueError:
        max_lookups = _DEFAULT_MAX_LOOKUPS
    budget = entity_enrich.Budget(max_lookups)
    llm = entity_classify.codex_llm(_MAX_CLASSIFY_CALLS, _CLASSIFY_TIMEOUT_S,
                                    started + _CLASSIFY_DEADLINE_S)
    town = town_name()

    kinds = list(bd.KINDS) if args.kind == "all" else [args.kind]

    def run(conn, client, profile, slug):
        out = {"processed": 0, "promoted": 0, "already_live": 0, "deferred": 0, "failed": 0,
               "withdrawn": 0, "shipped": [], "errors": []}
        for kind in kinds:
            ekind = bp.entity_kind(kind)

            def enrich(conn, entity_uid, ekind=ekind):
                return entity_enrich.enrich_entity(conn, ekind, entity_uid, town, budget,
                                                   llm).status
            part = bp.promote_approved(conn, client, slug, enrich=enrich, limit=args.limit,
                                       kind=kind)
            for k, v in part.items():
                out[k] = v if k == "held" else out[k] + v
        return out
    return _locked_run("promoted", run)


def _stats(args) -> int:
    print(json.dumps(bd.stats(open_db(), _town_slug(), kind=args.kind), indent=2))
    return 0


def main(argv=None) -> int:
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
    stats = sub.add_parser("stats")
    for name in ("propose-promote", "propose-decline"):
        pp = sub.add_parser(name)
        pp.add_argument("--uid", action="append", default=None,
                        help="candidate uid (or prefix); repeatable. Default: all eligible")
        pp.add_argument("--limit", type=int, default=50)
    promote = sub.add_parser("promote")
    promote.add_argument("--limit", type=int, default=3)
    harvest = sub.add_parser("harvest")
    harvest.add_argument("--per-source", type=int, default=15)
    harvest.add_argument("--limit", type=int, default=10)
    for sp, kinds in ((sweep, bd.KINDS), (verify, bd.KINDS), (ls, bd.KINDS), (stats, bd.KINDS),
                      (promote, (*bd.KINDS, "all"))):
        sp.add_argument("--kind", default="all" if sp is promote else bd.KIND,
                        choices=list(kinds))
    harvest.set_defaults(kind="all")
    args = ap.parse_args(argv)
    if not _town_slug():
        print("TOWNLOOPS_TOWN_SLUG not set", file=sys.stderr)
        return 1
    return {"sweep": _sweep, "verify": _verify, "list": _list, "stats": _stats,
            "propose-promote": _propose("promote"), "propose-decline": _propose("decline"),
            "promote": _promote, "harvest": _harvest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
