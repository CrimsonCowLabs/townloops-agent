#!/usr/bin/env python3
"""venue-enrichment — see engine/enrichment.py for the shared loop."""
import argparse, json, os, sys
sys.path.insert(0, "/app")
from engine import enrichment  # noqa: E402

KIND = "business"


def cmd_sweep(args) -> int:
    ids = enrichment.claim(KIND, args.limit)
    if not ids:
        print(json.dumps({"kind": KIND, "claimed": 0, "queue_depth": enrichment.queue_depth(KIND)}))
        return 0
    results = enrichment.apply(KIND, ids, use_firecrawl=args.firecrawl)
    print(json.dumps({"kind": KIND, "claimed": len(ids), "results": results}, indent=2))
    return 0


def cmd_ship(args) -> int:
    print(json.dumps(enrichment.ship(KIND, batch=args.batch), indent=2))
    return 0


def cmd_depth(args) -> int:
    print(json.dumps({"kind": KIND, "queue_depth": enrichment.queue_depth(KIND)}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sweep")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--firecrawl", action="store_true", help="fall back to Firecrawl on fetch failure")
    s.set_defaults(func=cmd_sweep)
    sh = sub.add_parser("ship")
    sh.add_argument("--batch", type=int, default=100)
    sh.set_defaults(func=cmd_ship)
    sub.add_parser("depth").set_defaults(func=cmd_depth)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
