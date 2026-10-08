"""Workqueue for the event-first pipeline.

Semantics:
- `enqueue(event_uid, action)`: idempotent insert (uses UNIQUE constraint).
- `claim(action, worker, limit)`: mark N rows as claimed by this worker
  atomically. Returns the claimed rows for processing.
- `complete(queue_id)`: mark resolved. Worker calls after successful transition.
- `fail(queue_id, error)`: increment attempts; if >= MAX_ATTEMPTS, mark
  the event stuck (via engine.event_state.mark_stuck) and resolve the row.

Rate limiting is per-action + per-hour, read from town-profile.yaml if
present. Defaults are conservative for a ChatGPT Plus tier.

Every claim/complete/fail also writes to `agent_activity` via engine.inspection
so the dashboard's live ticker sees what's happening.
"""

from __future__ import annotations

import os
import socket
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from engine import event_state


MAX_ATTEMPTS = 3

# Default per-action-per-hour limits. Overridden by town-profile.yaml
# workqueue_rate_limits.<action>.
_DEFAULT_RATE_LIMITS = {
    "read-detail":         30,
    "extract-venue":       50,
    "extract-hosts":       50,
    "extract-people":      50,
    "enrich-entities":    100,
    "ship":               100,
}


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _worker_id() -> str:
    host = os.environ.get("TOWNLOOPS_AGENT_HOSTNAME") or socket.gethostname()
    return f"{host}:{os.getpid()}"


def _load_rate_limits() -> dict[str, int]:
    """Read workqueue_rate_limits from town-profile.yaml, falling back to
    the defaults for any missing action."""
    import yaml
    from pathlib import Path
    p = Path(os.environ.get(
        "TOWN_PROFILE_PATH",
        os.path.join(os.environ.get("HERMES_HOME", "/home/agent/.hermes"),
                     "town-profile.yaml"),
    ))
    if not p.exists():
        return dict(_DEFAULT_RATE_LIMITS)
    try:
        doc = yaml.safe_load(p.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return dict(_DEFAULT_RATE_LIMITS)
    limits = dict(_DEFAULT_RATE_LIMITS)
    override = (doc.get("workqueue_rate_limits") or {})
    for k, v in override.items():
        if isinstance(v, int):
            limits[k] = v
    return limits


def enqueue(conn: sqlite3.Connection, event_uid: str, action: str) -> Optional[int]:
    """Idempotent: if a row already exists for this (event_uid, action),
    reset it to unclaimed (in case the previous worker died). Returns
    queue_id or None if we couldn't enqueue."""
    now = _iso_utc_now()
    cur = conn.execute(
        """
        INSERT INTO event_workqueue(event_uid, next_action, enqueued_at)
        VALUES (?, ?, ?)
        ON CONFLICT (event_uid, next_action) DO UPDATE SET
            claimed_by = NULL,
            claimed_at = NULL,
            resolved_at = NULL,
            last_error = NULL
        """,
        (event_uid, action, now),
    )
    row = conn.execute(
        "SELECT queue_id FROM event_workqueue WHERE event_uid = ? AND next_action = ?",
        (event_uid, action),
    ).fetchone()
    return row[0] if row else None


def _hour_window_usage(conn: sqlite3.Connection, action: str) -> int:
    """How many claims of this action have started in the last hour?"""
    row = conn.execute(
        """
        SELECT COUNT(*) FROM event_workqueue
        WHERE next_action = ?
          AND claimed_at IS NOT NULL
          AND claimed_at >= datetime('now', '-1 hour')
        """,
        (action,),
    ).fetchone()
    return row[0] if row else 0


def claim(conn: sqlite3.Connection, action: str, limit: int = 5) -> list[dict]:
    """Claim up to `limit` unresolved rows for `action`, honoring per-hour
    rate limit. Returns the claimed rows (with event details joined)."""
    limits = _load_rate_limits()
    hour_cap = limits.get(action, 100)
    already_this_hour = _hour_window_usage(conn, action)
    budget = max(0, hour_cap - already_this_hour)
    if budget == 0:
        return []
    take = min(limit, budget)

    conn.row_factory = sqlite3.Row
    candidates = conn.execute(
        """
        SELECT queue_id, event_uid FROM event_workqueue
        WHERE next_action = ?
          AND resolved_at IS NULL
          AND (claimed_at IS NULL OR attempts > 0)
          AND attempts < ?
        ORDER BY enqueued_at
        LIMIT ?
        """,
        (action, MAX_ATTEMPTS, take),
    ).fetchall()

    if not candidates:
        return []

    worker = _worker_id()
    now = _iso_utc_now()
    claimed = []
    for c in candidates:
        # Atomic claim: rowcount 0 means someone else grabbed it.
        cur = conn.execute(
            """
            UPDATE event_workqueue
            SET claimed_by = ?, claimed_at = ?
            WHERE queue_id = ?
              AND (claimed_at IS NULL OR attempts > 0)
              AND resolved_at IS NULL
            """,
            (worker, now, c["queue_id"]),
        )
        if cur.rowcount > 0:
            evt = conn.execute(
                "SELECT * FROM events WHERE event_uid = ?", (c["event_uid"],),
            ).fetchone()
            if evt:
                d = dict(evt)
                d["queue_id"] = c["queue_id"]
                claimed.append(d)
    conn.commit()
    return claimed


def complete(conn: sqlite3.Connection, queue_id: int) -> None:
    conn.execute(
        "UPDATE event_workqueue SET resolved_at = ? WHERE queue_id = ?",
        (_iso_utc_now(), queue_id),
    )
    conn.commit()


def fail(conn: sqlite3.Connection, queue_id: int, error: str) -> None:
    """Increment attempts; if reached MAX_ATTEMPTS, resolve the row AND
    mark the event stuck so it surfaces to the operator."""
    now = _iso_utc_now()
    conn.execute(
        """
        UPDATE event_workqueue
        SET attempts = attempts + 1,
            last_error = ?,
            last_attempt_at = ?,
            claimed_by = NULL,
            claimed_at = NULL
        WHERE queue_id = ?
        """,
        (error[:1000], now, queue_id),
    )
    row = conn.execute(
        "SELECT event_uid, attempts FROM event_workqueue WHERE queue_id = ?",
        (queue_id,),
    ).fetchone()
    if row and row[1] >= MAX_ATTEMPTS:
        event_state.mark_stuck(conn, row[0])
        conn.execute(
            "UPDATE event_workqueue SET resolved_at = ? WHERE queue_id = ?",
            (now, queue_id),
        )
    conn.commit()


def is_last_attempt(conn: sqlite3.Connection, event_uid: str, action: str) -> bool:
    """True when a failure now would be the event's last attempt at `action`
    (fail() then marks it stuck) — or there is no queue row (a by-hand run)."""
    row = conn.execute(
        "SELECT attempts FROM event_workqueue WHERE event_uid = ? AND next_action = ?",
        (event_uid, action),
    ).fetchone()
    return row is None or row[0] >= MAX_ATTEMPTS - 1


def park(conn: sqlite3.Connection, event_uid: str) -> None:
    """Hand an event to the operator now, without spending its remaining
    attempts: mark it stuck and resolve its open queue rows so nothing
    retries it before a human has looked."""
    event_state.mark_stuck(conn, event_uid)
    conn.execute(
        "UPDATE event_workqueue SET resolved_at = ? "
        "WHERE event_uid = ? AND resolved_at IS NULL",
        (_iso_utc_now(), event_uid),
    )
    conn.commit()


def reap_stale_claims(conn: sqlite3.Connection,
                      older_than_minutes: int = 15) -> int:
    """Reclaim rows whose worker claimed them but never resolved them.

    `claim()` commits `claimed_at` (attempts=0) before the orchestrator runs the
    skill. If the drainer dies in between (deploy SIGTERM, OOM, host restart), the
    row is orphaned: it can't be re-claimed (the claim filter needs
    `claimed_at IS NULL OR attempts > 0`) and never surfaces to the operator (the
    dashboard only shows attempts >= MAX_ATTEMPTS). Route each stale claim through
    `fail()` so it either re-enters the queue or, after MAX_ATTEMPTS reaps, gets
    marked `stuck-` and becomes visible. Returns the number of rows reaped.
    """
    stale = conn.execute(
        """
        SELECT queue_id FROM event_workqueue
        WHERE resolved_at IS NULL
          AND claimed_at IS NOT NULL
          AND claimed_at < datetime('now', ?)
        """,
        (f"-{int(older_than_minutes)} minutes",),
    ).fetchall()
    for r in stale:
        fail(conn, r[0], f"reclaimed: stale claim (worker died?) after "
                         f"{older_than_minutes}m")
    return len(stale)


def counts_by_action(conn: sqlite3.Connection) -> dict[str, dict]:
    """{action: {pending, in_flight, failed, resolved}} — powers the
    dashboard's workqueue-depth bar."""
    rows = conn.execute(
        """
        SELECT next_action AS action,
               SUM(CASE WHEN resolved_at IS NULL AND claimed_at IS NULL THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN resolved_at IS NULL AND claimed_at IS NOT NULL AND attempts = 0 THEN 1 ELSE 0 END) AS in_flight,
               SUM(CASE WHEN resolved_at IS NULL AND attempts > 0 AND attempts < ? THEN 1 ELSE 0 END) AS retrying,
               SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolved
        FROM event_workqueue
        GROUP BY next_action
        """,
        (MAX_ATTEMPTS,),
    ).fetchall()
    return {
        r["action"]: {
            "pending": r["pending"] or 0,
            "in_flight": r["in_flight"] or 0,
            "retrying": r["retrying"] or 0,
            "resolved": r["resolved"] or 0,
        }
        for r in rows
    }
