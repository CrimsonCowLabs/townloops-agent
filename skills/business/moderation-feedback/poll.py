#!/usr/bin/env python3
"""moderation-feedback — read back what the TownLoops moderator did with each
record the agent submitted in autonomous mode (published, rejected, merged
or gone) and learn from it: seed trust, Series rejection streaks, remembered
rejections, and the rejection-rate circuit breaker. Logic: engine/moderation.py.

Usage: poll.py [--limit N]     (cron, hourly; a no-op with nothing pending)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from engine import autonomy, moderation
from engine import lock as _lock
from engine.derive_helpers import open_db, record_activity

_WORKER = "moderation-feedback"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="moderation-feedback")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args(argv)
    conn = open_db()
    if not autonomy.pending_count(conn):
        print(json.dumps({"checked": 0, "note": "no pending submissions"}))
        return 0
    if not all(os.environ.get(k) for k in
               ("TOWNLOOPS_API_URL", "TOWNLOOPS_API_KEY", "TOWNLOOPS_TOWN_SLUG")):
        print("TownLoops env unset — nothing to poll")
        return 0
    from engine.townloops_api import Client
    try:
        with _lock.acquire(_WORKER):
            client = Client()
            client.verify_town()
            out = moderation.poll(client, conn, autonomy.settings(), limit=args.limit)
    except _lock.LockHeld:
        print("previous moderation-feedback run still active — skipped")
        return 0
    record_activity(conn, _WORKER, "poll", "submissions",
                    detail=json.dumps({k: v for k, v in out.items() if k != "errors"}),
                    ok=not out["errors"], error="; ".join(out["errors"])[:1000] or None)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
