"""Cron job health: alert when a scheduled job fails or is skipped on two
runs in a row — once, on the transition, like engine/source_health — and
again ("ok again") when it next succeeds.

    (ok) ──2nd non-ok run in a row──▶ alerted ──an ok run──▶ (ok)

Two sources feed `record`, one `job_health` row per job:

- Hermes' own record of every cron run: `$HERMES_HOME/cron/output/<job
  id>/<YYYY-MM-DD_HH-MM-SS>.md`, its `**Status:**` line saying `script
  failed` (an import error, a non-zero exit). `scan_hermes` reads the files
  it hasn't seen yet — the heartbeat's tick (engine/alerts) runs it every 10
  minutes. Jobs are keyed `cron:<job id>`.
- Skills a detached wrapper starts, whose skip Hermes never sees (it saw the
  wrapper start fine): they call `record` with their own name — a run that
  found its lock held is `skipped` (entity-live-sync, business-discovery).

`standing` lists the jobs with an alert out, for the daily report.
Standard library only (the heartbeat wrapper runs under the gateway venv).
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Callable, Optional

from engine.derive_helpers import dict_rows as _rows, iso_utc_now

OK, FAILED, SKIPPED = "ok", "failed", "skipped"
ALERT_AFTER = 2              # non-ok runs in a row
SCAN_BACKLOG = 10            # files read per job on a first scan

_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.md$")
_STATUS = re.compile(r"^\*\*Status:\*\*\s*(.+)$", re.MULTILINE)


def record(conn: sqlite3.Connection, job: str, outcome: str, *, detail: str = "",
           run_at: Optional[str] = None,
           alert: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """One run of `job`. Returns "alerted" / "recovered" when that transition
    happened (the operator was told), else None. A `run_at` (a scanned run)
    no newer than the last one recorded is ignored; without one the run is
    now. Commits."""
    row = conn.execute("SELECT last_run_at, last_outcome, streak, alerted_at FROM job_health "
                       "WHERE job = ?", (job,)).fetchone()
    if run_at and row and run_at <= row[0]:
        return None
    run_at = run_at or iso_utc_now()
    streak = 0 if outcome == OK else (row[2] if row else 0) + 1
    alerted_at, change = (row[3] if row else None), None
    if outcome == OK and alerted_at:
        alerted_at, change = None, "recovered"
        _send(alert, f"[{_town()}] cron job {job} ok again")
    elif streak >= ALERT_AFTER and not alerted_at:
        alerted_at, change = iso_utc_now(), "alerted"
        _send(alert, f"[{_town()}] cron job {job} {outcome} on {streak} runs in a row"
                     + (f": {detail}" if detail else ""))
    conn.execute(
        "INSERT INTO job_health (job, last_run_at, last_outcome, streak, detail, alerted_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (job) DO UPDATE SET "
        "last_run_at = excluded.last_run_at, last_outcome = excluded.last_outcome, "
        "streak = excluded.streak, detail = excluded.detail, alerted_at = excluded.alerted_at",
        (job, run_at, outcome, streak, (detail or "")[:400], alerted_at))
    conn.commit()
    return change


def scan_hermes(conn: sqlite3.Connection, cron_dir: Optional[Path] = None,
                alert: Optional[Callable[[str], None]] = None) -> list[dict]:
    """Record the Hermes cron runs not seen yet, oldest first. Returns the
    transitions ({job, change})."""
    out_dir = (cron_dir or Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes"))
               / "cron") / "output"
    if not out_dir.is_dir():
        return []
    changes = []
    for job_dir in sorted(p for p in out_dir.iterdir() if p.is_dir()):
        job = f"cron:{job_dir.name}"
        last = conn.execute("SELECT last_run_at FROM job_health WHERE job = ?",
                            (job,)).fetchone()
        runs = []
        for f in job_dir.iterdir():
            m = _STAMP.match(f.name)
            if m:
                at = f"{m[1]}T{m[2]}:{m[3]}:{m[4]}"
                if not last or at > last[0]:
                    runs.append((at, f))
        for at, f in sorted(runs)[-SCAN_BACKLOG:]:
            outcome, detail = _hermes_outcome(f)
            change = record(conn, job, outcome, detail=detail, run_at=at, alert=alert)
            if change:
                changes.append({"job": job, "change": change})
    return changes


def standing(conn: sqlite3.Connection) -> list[dict]:
    """Jobs with an alert out, longest-standing first."""
    return _rows(conn, "SELECT * FROM job_health WHERE alerted_at IS NOT NULL "
                       "ORDER BY alerted_at, job")


def _hermes_outcome(path: Path) -> tuple[str, str]:
    """(outcome, detail) of one Hermes cron output file: failed when its
    Status line says so, the detail its last line (the exception)."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return OK, ""
    m = _STATUS.search(text)
    status = (m[1] if m else "").lower()
    if not any(w in status for w in ("fail", "error", "timeout", "timed out")):
        return OK, ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return FAILED, (lines[-1] if lines else status)[:300]


def _send(alert: Optional[Callable[[str], None]], text: str) -> None:
    if alert:
        alert(text)
        return
    from engine import notify
    notify.telegram(text)


def _town() -> str:
    return os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")
