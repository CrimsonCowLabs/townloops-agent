#!/usr/bin/env python3
"""operator-status — headline counts snapshot for the operator chat.

Invoked by Hermes when the operator asks generic health/state questions.
Read-only. Uses engine.db.connect (available in the agent container);
the `dashboard/` package with its db_v2 helpers is NOT mounted here, so
we run the small amount of SQL directly.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone


def main() -> int:
    from engine import db as _engine_db  # imported lazily for clean --help behavior
    from engine import event_state

    with _engine_db.connect() as c:
        pending  = c.execute("SELECT COUNT(*) FROM events WHERE approval_status='pending'").fetchone()[0]
        approved = c.execute("SELECT COUNT(*) FROM events WHERE approval_status='approved'").fetchone()[0]
        declined = c.execute("SELECT COUNT(*) FROM events WHERE approval_status='declined'").fetchone()[0]
        shipped  = c.execute("SELECT COUNT(*) FROM events WHERE state='shipped'").fetchone()[0]
        in_flight = c.execute(
            f"SELECT COUNT(*) FROM events WHERE state IN "
            f"({','.join('?' for _ in event_state.IN_FLIGHT_STATES)})",
            event_state.IN_FLIGHT_STATES,
        ).fetchone()[0]

        seed_rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM event_seeds GROUP BY status"
        ).fetchall()
        seeds = {"proposed": 0, "approved": 0, "rejected": 0}
        for r in seed_rows:
            seeds[r["status"]] = r["n"]

        wq_active = c.execute(
            "SELECT COUNT(*) FROM event_workqueue WHERE resolved_at IS NULL"
        ).fetchone()[0]
        wq_stuck = c.execute(
            "SELECT COUNT(*) FROM event_workqueue WHERE resolved_at IS NULL AND attempts >= 3"
        ).fetchone()[0]
        wq_rows = c.execute(
            "SELECT next_action, COUNT(*) AS n FROM event_workqueue "
            "WHERE resolved_at IS NULL GROUP BY next_action"
        ).fetchall()
        wq_by_action = {r["next_action"]: r["n"] for r in wq_rows}

    payload = {
        "town": os.environ.get("TOWNLOOPS_TOWN_SLUG", "unknown"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "events": {
            "pending":   pending,
            "approved":  approved,
            "declined":  declined,
            "shipped":   shipped,
            "in_flight": in_flight,
        },
        "seeds": seeds,
        "workqueue": {
            "active":     wq_active,
            "stuck":      wq_stuck,
            "by_action":  wq_by_action,
        },
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
