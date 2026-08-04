"""Event state-machine transitions with optimistic locking.

Each event moves through a linear state chain. Workers advance events by
attempting UPDATE ... WHERE state = <old_state>; rowcount == 0 means
another worker (or the operator) already advanced it — the losing worker
skips silently. This is the whole concurrency-safety story.

Legal states + next-actions:

    found                       → (legacy event-ingest default; not serviced here)
    metadata-extracted          → (operator decides)
    approved                    → extract-venue
    declined                    → (terminal)
    venue-derived               → extract-hosts
    hosts-derived               → extract-people
    people-derived              → enrich-entities
    entities-enriched           → ship
    shipped                     → (terminal)
    stuck-<original_state>      → (operator manual override)

Approval status is a separate axis, stored on `events.approval_status`
(pending / approved / declined). State machine cares only about `state`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable


# Machine-readable transition table. Key: current state. Value: (next_action, next_state_on_success)
#
# There is no `found → …` transition: event-find inserts events directly at
# state=metadata-extracted. `found` remains only as the schema default for legacy
# event-ingest rows (which ship via their own path, outside this workqueue).
_TRANSITIONS: dict[str, tuple[str, str]] = {
    "approved":           ("extract-venue",    "venue-derived"),
    "venue-derived":      ("extract-hosts",    "hosts-derived"),
    "hosts-derived":      ("extract-people",   "people-derived"),
    "people-derived":     ("enrich-entities",  "entities-enriched"),
    "entities-enriched":  ("ship",             "shipped"),
}

TERMINAL_STATES = {"declined", "shipped"}
AWAITING_OPERATOR_STATES = {"metadata-extracted"}


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def next_action_for(state: str) -> str | None:
    """Return the workqueue action that advances an event out of `state`,
    or None if the event is terminal or awaiting the operator."""
    t = _TRANSITIONS.get(state)
    return t[0] if t else None


def next_state_for(action: str) -> str | None:
    """Reverse lookup: which state does `action` produce?"""
    for cur, (act, nxt) in _TRANSITIONS.items():
        if act == action:
            return nxt
    return None


def attempt_advance(conn: sqlite3.Connection, event_uid: str,
                    from_state: str, to_state: str) -> bool:
    """Optimistic advance: succeeds iff the event is still in `from_state`.
    Returns True on advance, False if the row didn't match (someone else
    already moved it, or it doesn't exist)."""
    cur = conn.execute(
        "UPDATE events SET state = ?, state_updated_at = ? "
        "WHERE event_uid = ? AND state = ?",
        (to_state, iso_utc_now(), event_uid, from_state),
    )
    return cur.rowcount > 0


def mark_stuck(conn: sqlite3.Connection, event_uid: str) -> None:
    """Prefix the current state with 'stuck-' so it disappears from the
    workqueue and surfaces on the operator's failed-panel."""
    row = conn.execute(
        "SELECT state FROM events WHERE event_uid = ?", (event_uid,),
    ).fetchone()
    if not row:
        return
    cur_state = row["state"] if isinstance(row, sqlite3.Row) else row[0]
    if cur_state.startswith("stuck-"):
        return
    conn.execute(
        "UPDATE events SET state = ?, state_updated_at = ? WHERE event_uid = ?",
        (f"stuck-{cur_state}", iso_utc_now(), event_uid),
    )


def unstuck(conn: sqlite3.Connection, event_uid: str) -> str | None:
    """Operator requeue: strip the 'stuck-' prefix; return the restored
    state so the caller can enqueue the appropriate next-action."""
    row = conn.execute(
        "SELECT state FROM events WHERE event_uid = ?", (event_uid,),
    ).fetchone()
    if not row:
        return None
    cur_state = row["state"] if isinstance(row, sqlite3.Row) else row[0]
    if not cur_state.startswith("stuck-"):
        return cur_state
    restored = cur_state[len("stuck-"):]
    conn.execute(
        "UPDATE events SET state = ?, state_updated_at = ? WHERE event_uid = ?",
        (restored, iso_utc_now(), event_uid),
    )
    return restored


def event_awaiting_operator(conn: sqlite3.Connection) -> Iterable[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM events "
        "WHERE state = 'metadata-extracted' AND approval_status = 'pending' "
        "ORDER BY date ASC"
    ).fetchall()
    return [dict(r) for r in rows]
