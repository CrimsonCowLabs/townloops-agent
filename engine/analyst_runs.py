"""The daily analyst run's bookkeeping and budget (#64): one `analyst_runs`
row per run. The run asks for its remaining budget here (the town-report
skill's `budget` verbs) instead of keeping count itself.

    start ──▶ running ──finish──▶ ok | failed
                 └── still running past its max_minutes at the next start ──▶ interrupted

The budget is town-profile.yaml's `analyst:` block over `DEFAULTS`. Used:
minutes since start, Firecrawl requests since start (counted per request by
engine/firecrawl_env), and submissions to TownLoops since start
(`submissions`). The numbers are advisory — the run stops itself when one
runs out; the submission gate and the Firecrawl token bucket still apply.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from engine import firecrawl_env
from engine.derive_helpers import iso_utc_now, load_town_profile, parse_instant

DEFAULTS = {"max_minutes": 90, "max_firecrawl": 300, "max_submissions": 150}
STATUSES = ("running", "ok", "failed", "interrupted")
_USED = {"max_minutes": "minutes", "max_firecrawl": "firecrawl",
         "max_submissions": "submissions"}


class RunActive(RuntimeError):
    """Another analyst run is still within its time."""


def budget_from(profile: Optional[dict] = None) -> dict:
    """`analyst:` in town-profile.yaml over the defaults (bad values ignored)."""
    a = ((load_town_profile() if profile is None else profile) or {}).get("analyst") or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        try:
            if a.get(k) is not None and int(a[k]) > 0:
                out[k] = int(a[k])
        except (TypeError, ValueError):
            pass
    return out


def start(conn: sqlite3.Connection, profile: Optional[dict] = None, *,
          backup: Optional[Callable[[], list[str]]] = None,
          now: Optional[datetime] = None) -> dict:
    """Open a run: earlier runs left running past their time are marked
    interrupted first; a run still within its time refuses (RunActive).
    `backup` (engine/local_data.backup) runs before the row is written."""
    now = now or datetime.now(timezone.utc)
    interrupted = []
    for r in conn.execute("SELECT id, started_at, budget_json FROM analyst_runs "
                          "WHERE status = 'running' ORDER BY id").fetchall():
        budget = _json(r[2])
        deadline = parse_instant(r[1]) + timedelta(
            minutes=budget.get("max_minutes") or DEFAULTS["max_minutes"])
        if deadline > now:
            raise RunActive(f"analyst run #{r[0]} is still running (started {r[1]})")
        conn.execute("UPDATE analyst_runs SET status = 'interrupted', ended_at = ?, "
                     "notes = COALESCE(notes || ' | ', '') || ? WHERE id = ? "
                     "AND status = 'running'",
                     (_iso(now), f"still running past {budget.get('max_minutes')} min", r[0]))
        interrupted.append(r[0])
    conn.commit()
    backups = backup() if backup else []
    budget = budget_from(profile)
    cur = conn.execute("INSERT INTO analyst_runs (started_at, status, budget_json, backup_json) "
                       "VALUES (?, 'running', ?, ?)",
                       (_iso(now), json.dumps(budget), json.dumps(backups)))
    conn.commit()
    return {"run_id": cur.lastrowid, "budget": budget, "backups": backups,
            "interrupted": interrupted}


def status(conn: sqlite3.Connection, run_id: int, now: Optional[datetime] = None) -> dict:
    """The run's budget, what it has used and what remains."""
    r = _row(conn, run_id)
    budget = _json(r["budget_json"])
    used = _json(r["used_json"]) if r["status"] != "running" else usage(conn, r, now)
    remaining = {_USED[k]: max(0, v - used[_USED[k]]) for k, v in budget.items() if k in _USED}
    return {"run_id": run_id, "status": r["status"], "started_at": r["started_at"],
            "ended_at": r["ended_at"], "budget": budget, "used": used,
            "remaining": remaining,
            "exhausted": sorted(k for k, v in remaining.items() if v <= 0)}


def finish(conn: sqlite3.Connection, run_id: int, status_: str, *,
           report: Optional[str] = None, notes: Optional[str] = None) -> dict:
    if status_ not in ("ok", "failed"):
        raise ValueError(f"finish status must be ok or failed, not {status_!r}")
    r = _row(conn, run_id)
    used = usage(conn, r)
    conn.execute("UPDATE analyst_runs SET status = ?, ended_at = ?, used_json = ?, "
                 "report_path = COALESCE(?, report_path), notes = COALESCE(?, notes) "
                 "WHERE id = ?", (status_, iso_utc_now(), json.dumps(used), report, notes, run_id))
    conn.commit()
    return status(conn, run_id)


def usage(conn: sqlite3.Connection, run: dict, now: Optional[datetime] = None) -> dict:
    since = run["started_at"]
    elapsed = ((now or datetime.now(timezone.utc)) - parse_instant(since)).total_seconds()
    return {"minutes": round(max(0.0, elapsed) / 60, 1),
            "firecrawl": firecrawl_env.calls_since(since),
            "submissions": conn.execute("SELECT COUNT(*) FROM submissions WHERE submitted_at >= ?",
                                        (since,)).fetchone()[0]}


def recent(conn: sqlite3.Connection, since: str) -> list[dict]:
    """Runs started or ended since `since`, newest first."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(
        "SELECT * FROM analyst_runs WHERE started_at >= ? OR ended_at >= ? ORDER BY id DESC",
        (since, since))]


def _row(conn: sqlite3.Connection, run_id: int) -> dict:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    r = cur.execute("SELECT * FROM analyst_runs WHERE id = ?", (run_id,)).fetchone()
    if r is None:
        raise KeyError(f"no analyst run #{run_id}")
    return dict(r)


def _json(raw: Optional[str]) -> dict:
    try:
        v = json.loads(raw or "{}")
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
