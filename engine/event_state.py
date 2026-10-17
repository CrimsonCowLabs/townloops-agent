"""Event state-machine transitions with optimistic locking.

Each event moves through a linear state chain. Workers advance events by
attempting UPDATE ... WHERE state = <old_state>; rowcount == 0 means
another worker (or the operator) already advanced it — the losing worker
skips silently. This is the whole concurrency-safety story.

Legal states + next-actions:

    found                       → (legacy event-ingest default; not serviced here)
    metadata-extracted          → (operator decides)
    approved                    → read-detail
    declined                    → (terminal)
    detail-read                 → extract-venue
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
    "approved":           ("read-detail",      "detail-read"),
    "detail-read":        ("extract-venue",    "venue-derived"),
    "venue-derived":      ("extract-hosts",    "hosts-derived"),
    "hosts-derived":      ("extract-people",   "people-derived"),
    "people-derived":     ("enrich-entities",  "entities-enriched"),
    "entities-enriched":  ("ship",             "shipped"),
}

TERMINAL_STATES = {"declined", "shipped"}
# Approved and on its way to ship (each has a workqueue action).
IN_FLIGHT_STATES = tuple(_TRANSITIONS)
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


def approve(conn: sqlite3.Connection, event_uid: str, note: str | None = None) -> bool:
    """Approve a pending event the way the dashboard does: state `approved`
    and the first workqueue action enqueued. False when it isn't pending any
    more (another worker or the operator decided first)."""
    from engine import workqueue
    now = iso_utc_now()
    cur = conn.execute(
        "UPDATE events SET approval_status = 'approved', approval_note = ?, approval_at = ?, "
        "  state = 'approved', state_updated_at = ? "
        "WHERE event_uid = ? AND approval_status = 'pending' AND state = 'metadata-extracted'",
        (note, now, now, event_uid))
    if cur.rowcount:
        workqueue.enqueue(conn, event_uid, next_action_for("approved"))
    return cur.rowcount > 0


def decline(conn: sqlite3.Connection, event_uid: str, note: str | None = None,
            from_states: tuple[str, ...] = ("metadata-extracted",)) -> bool:
    """Decline an event that is still in one of `from_states` (default: still
    pending). Its open queue rows are resolved."""
    now = iso_utc_now()
    marks = ", ".join("?" * len(from_states))
    cur = conn.execute(
        "UPDATE events SET approval_status = 'declined', approval_note = ?, approval_at = ?, "
        f"  state = 'declined', state_updated_at = ? WHERE event_uid = ? AND state IN ({marks})",
        (note, now, now, event_uid, *from_states))
    if cur.rowcount:
        conn.execute("UPDATE event_workqueue SET resolved_at = ? "
                     "WHERE event_uid = ? AND resolved_at IS NULL", (now, event_uid))
    return cur.rowcount > 0


def reopen_for_review(conn: sqlite3.Connection, event_uid: str, from_states: tuple[str, ...],
                      note: str | None = None, *, include_stuck: bool = False,
                      clear_venue: bool = False) -> bool:
    """Send an event that is still in one of `from_states` (or, with
    `include_stuck`, any `stuck-` state) back to the operator's approval
    queue: pending at `metadata-extracted`. The approval it had is gone
    (`approval_at` NULL) — only a new one counts. `note` replaces the
    approval note; `clear_venue` drops the derived venue. Its queue rows are
    resolved with their attempt counts reset, so the actions a new approval
    enqueues can be claimed. False when it had moved on (another worker or
    the operator decided first)."""
    now = iso_utc_now()
    marks = ", ".join("?" * len(from_states)) or "NULL"
    cur = conn.execute(
        "UPDATE events SET state = 'metadata-extracted', approval_status = 'pending', "
        "  approval_at = NULL, approval_note = COALESCE(?, approval_note), state_updated_at = ?, "
        "  derived_venue_uid = CASE WHEN ? THEN NULL ELSE derived_venue_uid END "
        f"WHERE event_uid = ? AND (state IN ({marks}) OR (? AND state LIKE 'stuck-%'))",
        (note, now, 1 if clear_venue else 0, event_uid, *from_states, 1 if include_stuck else 0))
    if not cur.rowcount:
        return False
    conn.execute("UPDATE event_workqueue SET attempts = 0, "
                 "  resolved_at = COALESCE(resolved_at, ?) WHERE event_uid = ?", (now, event_uid))
    return True


def event_awaiting_operator(conn: sqlite3.Connection) -> Iterable[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM events "
        "WHERE state = 'metadata-extracted' AND approval_status = 'pending' "
        "ORDER BY date ASC"
    ).fetchall()
    return [dict(r) for r in rows]
