"""Read helpers for the event-first dashboard (v2).

Sibling of `dashboard/db.py`; shares connect() but exposes helpers for the
event-first schema (events with approval_status/state, event_workqueue,
event_seeds, agent_activity). Kept separate so the entity-first dashboard
can keep working during the /v2 rollout window.

All writes on the operator's behalf go through `set_*` helpers here rather
than skill CLIs — approving flips state→approved AND enqueues the first
workqueue action (`read-detail`) in the SAME transaction, so the state and
the queue row commit or roll back together (no stranded events).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from dashboard.db import connect, _iso_utc_now
from engine import event_state as _event_state
from engine import workqueue as _workqueue


# --------------------------------------------------------------------------
# Band 1 — inspection window
# --------------------------------------------------------------------------

def workqueue_counts_by_action() -> list[dict]:
    """Distribution of pending work across next_action buckets.
    Excludes resolved entries."""
    with connect() as c:
        rows = c.execute(
            "SELECT next_action, COUNT(*) AS n "
            "FROM event_workqueue "
            "WHERE resolved_at IS NULL "
            "GROUP BY next_action "
            "ORDER BY n DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def event_counts_by_state() -> list[dict]:
    """How events are distributed across the state machine."""
    with connect() as c:
        rows = c.execute(
            "SELECT state, COUNT(*) AS n "
            "FROM events "
            "WHERE state IS NOT NULL "
            "GROUP BY state "
            "ORDER BY n DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def event_counts_by_approval() -> dict:
    """{'pending': N, 'approved': N, 'declined': N} — headline numbers."""
    with connect() as c:
        rows = c.execute(
            "SELECT COALESCE(approval_status, 'unset') AS s, COUNT(*) AS n "
            "FROM events "
            "GROUP BY s"
        ).fetchall()
    out = {"pending": 0, "approved": 0, "declined": 0, "shipped": 0, "unset": 0}
    for r in rows:
        out[r["s"]] = r["n"]
    with connect() as c:
        shipped = c.execute(
            "SELECT COUNT(*) FROM events WHERE state = 'shipped'"
        ).fetchone()
    out["shipped"] = shipped[0] if shipped else 0
    return out


def recent_activity(limit: int = 20) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT id, ts, worker, action, target, detail, duration_ms, ok, error "
            "FROM agent_activity "
            "ORDER BY id DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def current_agent_task() -> Optional[dict]:
    """Most-recent activity row with duration_ms IS NULL — best proxy for
    'agent is currently doing X'. Returns None if last few rows all completed."""
    with connect() as c:
        row = c.execute(
            "SELECT id, ts, worker, action, target, detail "
            "FROM agent_activity "
            "WHERE duration_ms IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# Band 2 — approval queue
# --------------------------------------------------------------------------

_EVENT_CARD_COLS = """
  event_uid, source, discovered_via,
  COALESCE(title, name) AS title,
  COALESCE(date_local, date) AS date_display,
  COALESCE(date_utc, date) AS date_sort,
  end_at_local, end_at,
  COALESCE(venue_name, venue_raw) AS venue,
  COALESCE(location, location_raw) AS location,
  description, category_slug, image_url, rsvp_url, price,
  metadata_confidence, in_town_confidence,
  approval_status, approval_note, approval_at,
  state, state_updated_at,
  first_seen_at, last_seen_at
"""


def events_by_approval(status: str, limit: int = 200) -> list[dict]:
    """status in {pending, approved, declined}. Shipped events use a
    different route (recently_shipped)."""
    if status not in ("pending", "approved", "declined"):
        raise ValueError(f"unknown approval status: {status}")
    # "pending" means awaiting operator review — always state=metadata-extracted.
    # Gating on it keeps this list identical to the state-machine / digest count
    # and excludes legacy event-ingest rows (which default to state='found').
    state_clause = " AND state = 'metadata-extracted'" if status == "pending" else ""
    with connect() as c:
        rows = c.execute(
            f"SELECT {_EVENT_CARD_COLS} "
            "FROM events "
            f"WHERE approval_status = ?{state_clause} "
            "ORDER BY COALESCE(date_utc, date, first_seen_at) ASC "
            "LIMIT ?",
            (status, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def event_by_uid_v2(uid: str) -> Optional[dict]:
    with connect() as c:
        row = c.execute(
            f"SELECT {_EVENT_CARD_COLS} FROM events WHERE event_uid = ?",
            (uid,),
        ).fetchone()
    return dict(row) if row else None


def set_event_approval(uid: str, status: str, note: Optional[str] = None) -> int:
    """Flip an event's approval_status AND advance state so the pipeline view is
    coherent. On approval, enqueue the first workqueue action (`read-detail`)
    in the SAME transaction, so an event can never end up `approved` with no
    queue row (there is no state-driven backfill to rescue it). Uses optimistic
    locking on approval_status so two concurrent approvals don't double-enqueue."""
    if status not in ("approved", "declined"):
        raise ValueError(f"invalid target status: {status}")
    now = _iso_utc_now()
    # `approved` state feeds the first workqueue action; `declined` is terminal.
    new_state = "approved" if status == "approved" else "declined"
    with connect() as c:
        # connect() is autocommit (isolation_level=None), so wrap the flip + the
        # enqueue in an explicit transaction — otherwise a failed enqueue would
        # leave the event approved with no queue row.
        c.execute("BEGIN")
        try:
            cur = c.execute(
                "UPDATE events "
                "SET approval_status = ?, approval_note = ?, approval_at = ?, "
                "    state = ?, state_updated_at = ? "
                "WHERE event_uid = ? AND approval_status = 'pending'",
                (status, note, now, new_state, now, uid),
            )
            if cur.rowcount and status == "approved":
                _workqueue.enqueue(c, uid, _event_state.next_action_for("approved"))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return cur.rowcount


# --------------------------------------------------------------------------
# Band 3 — in-flight & recently shipped
# --------------------------------------------------------------------------

_IN_FLIGHT_STATES = _event_state.IN_FLIGHT_STATES


def events_in_flight(limit: int = 50) -> list[dict]:
    with connect() as c:
        placeholders = ",".join("?" for _ in _IN_FLIGHT_STATES)
        rows = c.execute(
            f"SELECT event_uid, COALESCE(title, name) AS title, "
            "  state, state_updated_at, approval_at, "
            "  COALESCE(date_local, date) AS date_display "
            f"FROM events WHERE state IN ({placeholders}) "
            "ORDER BY state_updated_at DESC LIMIT ?",
            (*_IN_FLIGHT_STATES, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def events_recently_shipped(limit: int = 20) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT event_uid, COALESCE(title, name) AS title, "
            "  COALESCE(date_local, date) AS date_display, "
            "  state_updated_at AS shipped_at, "
            "  townloops_slug, category_slug "
            "FROM events WHERE state = 'shipped' "
            "ORDER BY state_updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def workqueue_stuck(limit: int = 20) -> list[dict]:
    """Workqueue entries flagged stuck (attempts >= 3, unresolved)."""
    with connect() as c:
        rows = c.execute(
            "SELECT q.queue_id, q.event_uid, q.next_action, q.attempts, "
            "  q.last_error, q.last_attempt_at, "
            "  COALESCE(e.title, e.name) AS event_title "
            "FROM event_workqueue q "
            "LEFT JOIN events e ON e.event_uid = q.event_uid "
            "WHERE q.resolved_at IS NULL AND q.attempts >= 3 "
            "ORDER BY q.last_attempt_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def workqueue_requeue(queue_id: int) -> int:
    """Reset attempts + last_error so heartbeat retries. Returns rows affected."""
    with connect() as c:
        cur = c.execute(
            "UPDATE event_workqueue "
            "SET attempts = 0, last_error = NULL, claimed_by = NULL, "
            "    claimed_at = NULL, last_attempt_at = NULL "
            "WHERE queue_id = ? AND resolved_at IS NULL",
            (queue_id,),
        )
        return cur.rowcount


# --------------------------------------------------------------------------
# Band 4 — seeds & sources
# --------------------------------------------------------------------------

def event_seeds_by_status(status: str, limit: int = 200) -> list[dict]:
    """status in {proposed, approved, rejected, held}; held seeds are
    engine/seeds.held's list (with `hold_reason` and `hold_note`)."""
    if status not in ("proposed", "approved", "rejected", "held"):
        raise ValueError(f"unknown seed status: {status}")
    if status == "held":
        from engine.seeds import held
        with connect() as c:
            return held(c)
    with connect() as c:
        rows = c.execute(
            "SELECT seed_id, name, url, kind, status, discovered_by, "
            "  trust_auto_approve, last_scraped_at, last_event_count, "
            "  consecutive_empty_scrapes, notes, first_seen_at, "
            "  feed_type, feed_url, health_status, health_note "
            "FROM event_seeds WHERE status = ? "
            "ORDER BY name COLLATE NOCASE",
            (status,),
        ).fetchall()
    from engine.source_health import LABELS
    return [{**dict(r), "health_label": LABELS.get(r["health_status"])} for r in rows]


_SEED_VERDICTS = {"proposed": "propose", "approved": "approve", "rejected": "reject"}


def set_seed_status(seed_id: int, status: str) -> int:
    """The operator's seed approve/reject — a recorded status change through
    engine/seeds.set_status. Returns 1, or 0 for no such seed."""
    if status not in _SEED_VERDICTS:
        raise ValueError(f"invalid seed status: {status}")
    from engine import autonomy, seeds
    with connect() as c:
        row = c.execute("SELECT * FROM event_seeds WHERE seed_id = ?", (seed_id,)).fetchone()
        if not row:
            return 0
        seeds.set_status(c, dict(row), status, autonomy.Decision(
            _SEED_VERDICTS[status], "operator", {"was": row["status"]}))
        return 1


def ack_seed_health(seed_id: int) -> bool:
    """Operator accepts a seed's new event count (engine/source_health.ack)."""
    from engine import source_health
    with connect() as c:
        return source_health.ack(c, seed_id)


def seed_counts() -> dict:
    with connect() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM event_seeds GROUP BY status"
        ).fetchall()
    out = {"proposed": 0, "approved": 0, "rejected": 0, "held": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    return out


# --------------------------------------------------------------------------
# Empty-state cascade — walked when Band 2 (approval queue) is empty.
# Covers the branches from the plan's adversarial pass (#7).
# --------------------------------------------------------------------------

def empty_state_branch() -> Optional[dict]:
    """Return {branch, message, action_label, action_url, action_method} for
    the first blocked condition in the event-first cascade, or None if the
    pending queue is genuinely just quiet."""
    seeds = seed_counts()

    if seeds["approved"] == 0 and seeds["proposed"] == 0 and seeds["rejected"] == 0:
        return {
            "branch": "no_seeds",
            "message": "Seed discovery hasn't run yet. This is the first step "
                       "in the event-first pipeline — the agent finds source "
                       "pages (chambers, town gov, libraries) to scrape events from.",
            "action_label": "Discover seeds now",
            "action_url": "/v2/actions/discover-seeds",
            "action_method": "post",
        }

    if seeds["approved"] == 0 and seeds["proposed"] > 0:
        return {
            "branch": "seeds_awaiting_approval",
            "message": f"{seeds['proposed']} proposed seed sources are waiting "
                       "for your review. Approve some in Band 4 to start finding events.",
            "action_label": "Review seeds ↓",
            "action_url": "#seeds",
            "action_method": "anchor",
        }

    if seeds["approved"] > 0:
        with connect() as c:
            never_scraped = c.execute(
                "SELECT COUNT(*) FROM event_seeds "
                "WHERE status = 'approved' AND last_scraped_at IS NULL"
            ).fetchone()[0]
            never_scraped_all = never_scraped == seeds["approved"]
        if never_scraped_all:
            return {
                "branch": "seeds_never_scraped",
                "message": f"{seeds['approved']} seeds approved, none scraped yet. "
                           "The daily event-find cron runs at 05:00 UTC — or force it now.",
                "action_label": "Find events now",
                "action_url": "/v2/actions/event-find",
                "action_method": "post",
            }

    with connect() as c:
        found = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if found == 0:
        return {
            "branch": "no_events_yet",
            "message": f"{seeds['approved']} seeds have been scraped, but 0 events "
                       "were extracted. Feeds may be genuinely quiet, or the LLM "
                       "extractor may need a prompt tune.",
            "action_label": "Find events again",
            "action_url": "/v2/actions/event-find",
            "action_method": "post",
        }

    return None


# --------------------------------------------------------------------------
# Utility — event counts snapshot for header chips
# --------------------------------------------------------------------------

def headline_chips() -> dict:
    with connect() as c:
        seeds = c.execute(
            "SELECT COUNT(*) FROM event_seeds WHERE status = 'approved'"
        ).fetchone()[0]
        # Awaiting operator review: pending AND still at metadata-extracted.
        # Matches events_by_approval('pending') and the state-machine/digest count.
        pending = c.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE approval_status = 'pending' AND state = 'metadata-extracted'"
        ).fetchone()[0]
        in_flight = c.execute(
            f"SELECT COUNT(*) FROM events WHERE state IN "
            f"({','.join('?' for _ in _IN_FLIGHT_STATES)})", _IN_FLIGHT_STATES,
        ).fetchone()[0]
        shipped = c.execute(
            "SELECT COUNT(*) FROM events WHERE state = 'shipped'"
        ).fetchone()[0]
        wq_active = c.execute(
            "SELECT COUNT(*) FROM event_workqueue WHERE resolved_at IS NULL"
        ).fetchone()[0]
        wq_stuck = c.execute(
            "SELECT COUNT(*) FROM event_workqueue "
            "WHERE resolved_at IS NULL AND attempts >= 3"
        ).fetchone()[0]
    return {
        "seeds_approved": seeds,
        "pending": pending,
        "in_flight": in_flight,
        "shipped": shipped,
        "workqueue_active": wq_active,
        "workqueue_stuck": wq_stuck,
    }


def parse_tags(tags_json: Optional[str]) -> list[str]:
    if not tags_json:
        return []
    try:
        v = json.loads(tags_json)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []
