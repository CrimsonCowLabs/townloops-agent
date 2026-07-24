"""Shared CLI for the per-kind enrichment skills.

The three enrich.py files under skills/business/{venue,business,performer}-enrichment/
used to be near-verbatim copies with a single-line `KIND = ...` difference. They
now delegate here so bug fixes and new subcommands land once, not three times.

Each wrapper looks like:

    from engine.enrich_cli import run
    if __name__ == "__main__":
        raise SystemExit(run("venue"))

New subcommands (sweep, ship, depth, resync) added here are inherited by all
three skills automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from urllib.parse import urlparse

from engine import enrichment


def _cmd_sweep(kind: str, args) -> int:
    # Delegates to enrichment.sweep() which acquires an exclusive per-kind
    # lock so overlapping cron fires don't double-claim.
    result = enrichment.sweep(kind, args.limit, use_firecrawl=args.firecrawl)
    print(json.dumps(result, indent=2))
    return 0


def _cmd_ship(kind: str, args) -> int:
    print(json.dumps(enrichment.ship(kind, batch=args.batch), indent=2))
    return 0


def _cmd_depth(kind: str, args) -> int:
    print(json.dumps({"kind": kind, "queue_depth": enrichment.queue_depth(kind)}))
    return 0


def _cmd_resync(kind: str, args) -> int:
    """Pull remote → local for already-shipped rows so operator edits made
    directly on TownLoops don't get overwritten by the next enrichment sweep.
    See engine/enrichment.py:resync_from_remote for the full rationale."""
    result = enrichment.resync_from_remote(kind, limit=args.limit)
    print(json.dumps(result, indent=2))
    return 0 if not result.get("errors") else 3


def _cmd_cross_check(kind: str, args) -> int:
    """Peek at the CMO's townloops-research drafts.db for unshipped drafts of
    this kind. Read-only. See docs/business-discovery-handoff.md.

    Only meaningful for `business` today — CMO's discovery pipeline emits
    business candidates, not venues/performers.
    """
    path = os.environ.get("TOWNLOOPS_RESEARCH_DRAFTS_DB")
    if not path:
        print(json.dumps({
            "kind": kind,
            "configured": False,
            "note": "Set $TOWNLOOPS_RESEARCH_DRAFTS_DB to the CMO's townloops-research/data/drafts.db path.",
        }, indent=2))
        return 0
    if not os.path.exists(path):
        print(json.dumps({"kind": kind, "configured": True, "found": False,
                          "path": path, "note": "path set but file missing"}, indent=2))
        return 2
    # Open read-only (URI mode) — never touch the CMO's DB from here.
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        print(json.dumps({"kind": kind, "configured": True, "error": str(exc)}, indent=2))
        return 3
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT url, kind, payload_json, townloops_slug, saved_at, pushed_at "
        "FROM drafts WHERE kind = ? AND pushed_at IS NULL "
        "ORDER BY saved_at DESC",
        (kind,),
    ))
    town_hint = (os.environ.get("TOWNLOOPS_TOWN_SLUG") or "").lower()
    town_hint_name = args.town_hint or ""  # optional operator-supplied town name for the filter
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        name = payload.get("name") or ""
        address = payload.get("address") or ""
        # Heuristic town filter — payload has no town slug field, so we look
        # in the address text. If the operator hasn't asked to filter, list
        # everything and let them eyeball.
        if town_hint_name and town_hint_name.lower() not in address.lower():
            continue
        out.append({
            "url": r["url"],
            "name": name,
            "address": address,
            "saved_at": r["saved_at"],
            "host": urlparse(r["url"]).netloc,
        })
    print(json.dumps({
        "kind": kind,
        "town_slug": town_hint,
        "town_name_filter": town_hint_name or None,
        "drafts_db": path,
        "unshipped_business_drafts": len(out),
        "sample": out[:25],
        "note": "Drafts.db has no town-slug column. Operator ships from CMO side "
                "via research.py — this per-town agent does NOT ship them.",
    }, indent=2))
    return 0


def run(kind: str) -> int:
    """Entry point for the per-kind wrapper scripts."""
    if kind not in enrichment.SPECS:
        raise SystemExit(f"[enrich_cli] unknown kind: {kind}")

    p = argparse.ArgumentParser(prog=f"{kind}-enrichment")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sweep", help="claim + research queued rows")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--firecrawl", action="store_true",
                    help="fall back to Firecrawl on plain-fetch failure")
    s.set_defaults(func=lambda a: _cmd_sweep(kind, a))

    sh = sub.add_parser("ship", help="PATCH ready rows back to TownLoops")
    sh.add_argument("--batch", type=int, default=100)
    sh.set_defaults(func=lambda a: _cmd_ship(kind, a))

    d = sub.add_parser("depth", help="unclaimed queue depth")
    d.set_defaults(func=lambda a: _cmd_depth(kind, a))

    rs = sub.add_parser("resync",
                         help="pull remote → local for shipped rows (prevents "
                              "enrichment from clobbering operator edits made on TownLoops)")
    rs.add_argument("--limit", type=int, default=None,
                     help="cap on rows to resync (default: all shipped rows for this kind)")
    rs.set_defaults(func=lambda a: _cmd_resync(kind, a))

    if kind == "business":
        # Only meaningful for business — CMO's discovery pipeline emits
        # business candidates, not venues/performers.
        cc = sub.add_parser(
            "cross-check",
            help="peek at CMO's townloops-research drafts.db for unshipped business drafts",
        )
        cc.add_argument("--town-hint", dest="town_hint", default=None,
                         help="optional town-name substring to filter draft addresses by")
        cc.set_defaults(func=lambda a: _cmd_cross_check(kind, a))

    args = p.parse_args()
    return args.func(args)
