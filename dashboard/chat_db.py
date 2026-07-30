"""CRUD for the operator ↔ Hermes chat panel.

Sibling of `dashboard/db.py` and `dashboard/db_v2.py`. Shares the same
SQLite file, but opens its own connections with a longer busy_timeout
because three writers now contend for it: the heartbeat cron (agent
container), the dashboard's request handlers, and the detached
chat_worker subprocess. Every chat write is small and short-lived, so a
5s wait is plenty — WAL keeps readers unaffected.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


DEFAULT_THREAD_ID = "default"

_DB_PATH = Path(os.environ.get(
    "TOWNLOOPS_DB_PATH",
    os.path.join(
        os.environ.get("HERMES_HOME", "/home/agent/.hermes"),
        "state", "townloops.db",
    ),
))


def open_conn() -> sqlite3.Connection:
    """Open one connection with WAL + 5s busy timeout.

    busy_timeout on every open — three concurrent writers (heartbeat
    cron, FastAPI, chat_worker) will collide under load otherwise. WAL is
    already on from `engine.db.connect`, but set it explicitly here so
    this module works whether or not the DB was primed via that path.
    """
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def insert_message(role: str, body: str, *,
                   thread_id: str = DEFAULT_THREAD_ID,
                   hermes_session_id: Optional[str] = None,
                   hermes_run_id: Optional[str] = None,
                   tool_call_json: Optional[str] = None,
                   status: str = "delivered",
                   error: Optional[str] = None) -> int:
    """Insert one row; return the new message_id.

    role ∈ {operator, agent, system, tool}. Agent rows may be inserted
    with status='processing' as a placeholder while chat_worker runs;
    the worker then UPDATEs the row via `update_message_status`.
    """
    with open_conn() as c:
        cur = c.execute(
            "INSERT INTO chat_messages("
            "  thread_id, hermes_session_id, ts, role, body, "
            "  hermes_run_id, tool_call_json, status, error"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (thread_id, hermes_session_id, _iso_utc_now(), role, body,
             hermes_run_id, tool_call_json, status, error),
        )
        return cur.lastrowid


def update_message_status(message_id: int, *,
                          body: Optional[str] = None,
                          status: Optional[str] = None,
                          tool_call_json: Optional[str] = None,
                          error: Optional[str] = None,
                          hermes_run_id: Optional[str] = None,
                          expect_status: Optional[str] = None) -> int:
    """UPDATE a placeholder row. Optimistic-lock via `expect_status` so a
    late-arriving Hermes reply can't stomp a row already flipped by the
    orphan sweep. Returns rows affected (0 means the guard rejected)."""
    fields = []
    params: list = []
    if body is not None:
        fields.append("body = ?")
        params.append(body)
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if tool_call_json is not None:
        fields.append("tool_call_json = ?")
        params.append(tool_call_json)
    if error is not None:
        fields.append("error = ?")
        params.append(error)
    if hermes_run_id is not None:
        fields.append("hermes_run_id = ?")
        params.append(hermes_run_id)
    if not fields:
        return 0
    where = "WHERE message_id = ?"
    params.append(message_id)
    if expect_status is not None:
        where += " AND status = ?"
        params.append(expect_status)
    with open_conn() as c:
        cur = c.execute(
            f"UPDATE chat_messages SET {', '.join(fields)} {where}",
            params,
        )
        return cur.rowcount


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def history(thread_id: str = DEFAULT_THREAD_ID, *,
            since_id: Optional[int] = None,
            limit: int = 100) -> list[dict]:
    """Recent messages for a thread. `since_id` is exclusive — pass the
    latest message_id the client already has to fetch only new ones.

    Returns rows oldest-first (chat panel renders top-to-bottom)."""
    q = ("SELECT message_id, thread_id, hermes_session_id, ts, role, body, "
         "  hermes_run_id, tool_call_json, status, error "
         "FROM chat_messages WHERE thread_id = ?")
    params: list = [thread_id]
    if since_id is not None:
        q += " AND message_id > ?"
        params.append(since_id)
    # Grab most-recent N by id (fast index), then reverse for display.
    q += " ORDER BY message_id DESC LIMIT ?"
    params.append(limit)
    with open_conn() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in reversed(rows)]


def latest_message_id(thread_id: str = DEFAULT_THREAD_ID) -> int:
    with open_conn() as c:
        r = c.execute(
            "SELECT COALESCE(MAX(message_id), 0) FROM chat_messages "
            "WHERE thread_id = ?", (thread_id,),
        ).fetchone()
    return r[0] if r else 0


def latest_by_role(role: str,
                   thread_id: str = DEFAULT_THREAD_ID) -> Optional[dict]:
    with open_conn() as c:
        r = c.execute(
            "SELECT * FROM chat_messages WHERE thread_id = ? AND role = ? "
            "ORDER BY message_id DESC LIMIT 1", (thread_id, role),
        ).fetchone()
    return dict(r) if r else None


def get_message(message_id: int) -> Optional[dict]:
    with open_conn() as c:
        r = c.execute(
            "SELECT * FROM chat_messages WHERE message_id = ?", (message_id,),
        ).fetchone()
    return dict(r) if r else None


# --------------------------------------------------------------------------
# Thread session (X-Hermes-Session-Id)
# --------------------------------------------------------------------------

def thread_session_id(thread_id: str = DEFAULT_THREAD_ID) -> str:
    """Return this thread's stable Hermes session UUID; mint one on the
    first call and reuse forever after.

    Persisted on the newest chat row's `hermes_session_id`; if there are
    no rows yet we generate + return without writing (the next
    insert_message will pick it up when the caller passes it explicitly)."""
    with open_conn() as c:
        r = c.execute(
            "SELECT hermes_session_id FROM chat_messages "
            "WHERE thread_id = ? AND hermes_session_id IS NOT NULL "
            "ORDER BY message_id DESC LIMIT 1", (thread_id,),
        ).fetchone()
    if r and r["hermes_session_id"]:
        return r["hermes_session_id"]
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# Unread badge
# --------------------------------------------------------------------------

def unread_count(thread_id: str = DEFAULT_THREAD_ID) -> int:
    with open_conn() as c:
        last_seen = c.execute(
            "SELECT last_seen_message_id FROM chat_read_state "
            "WHERE thread_id = ?", (thread_id,),
        ).fetchone()
        cursor = last_seen["last_seen_message_id"] if last_seen else 0
        r = c.execute(
            "SELECT COUNT(*) FROM chat_messages "
            "WHERE thread_id = ? AND message_id > ? AND role != 'operator'",
            (thread_id, cursor),
        ).fetchone()
    return r[0] if r else 0


def mark_seen(thread_id: str = DEFAULT_THREAD_ID,
              up_to_message_id: Optional[int] = None) -> None:
    """Mark the thread read up to `up_to_message_id` (or the latest id
    on the thread if None). Idempotent — refuses to move backwards."""
    target = up_to_message_id if up_to_message_id is not None else latest_message_id(thread_id)
    if target <= 0:
        return
    with open_conn() as c:
        c.execute(
            "INSERT INTO chat_read_state(thread_id, last_seen_message_id) "
            "VALUES (?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET "
            "  last_seen_message_id = MAX(last_seen_message_id, excluded.last_seen_message_id)",
            (thread_id, target),
        )


# --------------------------------------------------------------------------
# Startup orphan sweep
# --------------------------------------------------------------------------

def reap_orphans(*, older_than_minutes: int = 5) -> int:
    """Rewrite 'processing' agent rows older than N minutes to 'failed'.

    Prevents "thinking…" bubbles frozen forever after a chat_worker died
    mid-response (container restart, OOM, uncaught exception before it
    could write its own failure). Called from the FastAPI startup hook.

    Returns count of rows swept.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=older_than_minutes)).isoformat(timespec="seconds")
    with open_conn() as c:
        cur = c.execute(
            "UPDATE chat_messages SET status = 'failed', "
            "  body = 'Worker died mid-response. Try again.', "
            "  error = 'orphan swept at startup' "
            "WHERE role = 'agent' AND status = 'processing' AND ts < ?",
            (cutoff,),
        )
        return cur.rowcount
