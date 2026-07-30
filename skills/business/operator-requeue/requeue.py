#!/usr/bin/env python3
"""operator-requeue — reset one stuck workqueue row so heartbeat retries it."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--queue-id", type=int, required=True)
    args = p.parse_args(argv)

    from engine import db as _engine_db

    with _engine_db.connect() as c:
        row = c.execute(
            "SELECT queue_id, event_uid, next_action, resolved_at "
            "FROM event_workqueue WHERE queue_id = ?", (args.queue_id,),
        ).fetchone()
        if not row:
            print(json.dumps({"queue_id": args.queue_id,
                              "requeued": False,
                              "error": "queue_id not found"}))
            return 0
        was_resolved = row["resolved_at"] is not None
        cur = c.execute(
            "UPDATE event_workqueue "
            "SET attempts=0, last_error=NULL, claimed_by=NULL, "
            "    claimed_at=NULL, last_attempt_at=NULL "
            "WHERE queue_id=? AND resolved_at IS NULL",
            (args.queue_id,),
        )
        c.commit()
        did_reset = cur.rowcount > 0

    print(json.dumps({
        "queue_id":     args.queue_id,
        "requeued":     did_reset,
        "was_resolved": was_resolved,
        "event_uid":    row["event_uid"],
        "next_action":  row["next_action"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
