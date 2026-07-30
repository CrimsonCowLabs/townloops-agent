"""Live-inspection recorder — writes to `agent_activity` so the dashboard's
ticker can show what the agent is doing right now.

Every LLM call, Firecrawl fetch, or major SQL mutation should record here.
Recorded rows are opaque to the pipeline; only the dashboard reads them.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Iterator, Optional


_HERMES_HOME = os.environ.get("HERMES_HOME", "/home/agent/.hermes")
_DB_PATH = Path(os.environ.get(
    "TOWNLOOPS_DB_PATH",
    os.path.join(_HERMES_HOME, "state", "townloops.db"),
))

# Sanitize contact PII before display: emails + phones. Real user descriptions
# often include "RSVP at foo@bar.com" or "(775) 555-1212".
_EMAIL_RE = re.compile(r'\b[\w.+-]+@[\w.-]+\.\w{2,}\b')
_PHONE_RE = re.compile(r'\b(?:\+?\d[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b')


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize(text: Optional[str]) -> str:
    if not text:
        return ""
    t = _EMAIL_RE.sub("<email>", text)
    t = _PHONE_RE.sub("<phone>", t)
    return t[:200]


def record(*, worker: str, action: str,
           target: Optional[str] = None,
           detail: Optional[str] = None,
           duration_ms: Optional[int] = None,
           ok: bool = True,
           error: Optional[str] = None) -> None:
    """One-shot recorder. Skills call this after any interesting action."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            """
            INSERT INTO agent_activity
                (ts, worker, action, target, detail, duration_ms, ok, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_iso_utc_now(), worker[:60], action[:40],
             (target or "")[:200], _sanitize(detail),
             duration_ms, 1 if ok else 0, (error or "")[:400]),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        # Recording must never break the caller.
        pass


@contextmanager
def timed(*, worker: str, action: str,
          target: Optional[str] = None,
          detail: Optional[str] = None) -> Iterator[dict]:
    """Context manager that records an activity with timing on exit.

        with inspection.timed(worker='event-find', action='firecrawl',
                              target=url, detail='fetching seed page') as ctx:
            body = fetch(url)
            ctx['ok'] = True  # optional; defaults True on clean exit
    """
    ctx = {"ok": True, "error": None, "detail": detail}
    started = monotonic()
    try:
        yield ctx
    except Exception as exc:
        ctx["ok"] = False
        ctx["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        duration_ms = int((monotonic() - started) * 1000)
        record(worker=worker, action=action, target=target,
               detail=ctx.get("detail") or detail,
               duration_ms=duration_ms,
               ok=bool(ctx.get("ok")),
               error=ctx.get("error"))


def recent(limit: int = 20) -> list[dict]:
    """Latest N activities, newest first. Powers the dashboard ticker."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM agent_activity ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def current_task() -> Optional[dict]:
    """The most recent unresolved (long-running) activity — what the agent
    is doing RIGHT NOW. If nothing recent, returns None (agent idle)."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agent_activity "
            "WHERE ts >= datetime('now', '-90 seconds') "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
