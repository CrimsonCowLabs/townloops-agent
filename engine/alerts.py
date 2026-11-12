"""Standing alerts — the one place that says what is wrong right now and
what needs the operator (#64). The daily report (engine/town_report) puts
them first, the nightly digest's "Needs attention" (engine/moderation)
takes the operator's share, and every report repeats them while they stand.

`tick` is the immediate half, run by the 10-minute heartbeat wrapper: the
circuit alert (once per opening, also for a circuit written straight into
the DB) and the cron-job health scan (engine/job_health). Both are
transition-only; the tick needs nothing beyond the standard library.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from engine import autonomy, job_health
from engine.derive_helpers import dict_rows as _rows

EXAMPLES = 5


@dataclass(frozen=True)
class Alert:
    kind: str            # circuit | api-freeze | paused | held | cancellation | job | source-health | analyst-run
    text: str
    needs_you: bool      # only the operator can clear it


def tick(conn: sqlite3.Connection, alert: Optional[Callable[[str], None]] = None) -> dict:
    """Send the alerts whose moment has come; returns what was sent."""
    return {"circuit": autonomy.alert_circuit(conn, alert),
            "jobs": job_health.scan_hermes(conn, alert=alert)}


def standing(conn: sqlite3.Connection) -> list[Alert]:
    """Everything standing now, the operator's items first."""
    out: list[Alert] = []
    s = autonomy.settings()
    c = autonomy.circuit(conn)
    if c:
        out.append(Alert("circuit", autonomy.circuit_text(c), True))
    freeze = _api_freeze()
    if freeze:
        out.append(Alert("api-freeze",
                         f"breaking TownLoops API change — writes to "
                         f"{', '.join(freeze.get('resources') or []) or '?'} frozen since "
                         f"{freeze.get('frozen_at')}: {freeze.get('reason')}. Update the payloads, "
                         "then clear state/api_freeze.json", True))
    if s.autonomous and s.paused:
        out.append(Alert("paused", "submissions paused (town-profile.yaml autonomy.paused) — "
                                   "unset it to resume", True))
    for j in job_health.standing(conn):
        out.append(Alert("job", f"cron job {j['job']} {j['last_outcome']} on {j['streak']} runs "
                                f"in a row (since {j['alerted_at']})"
                                + (f": {j['detail']}" if j["detail"] else ""), True))
    out += _held(conn)
    for r in _rows(conn, "SELECT COALESCE(title, name) AS title, date_local, townloops_slug, "
                         "cancel_note FROM events WHERE cancel_status = 'possibly-cancelled' "
                         "ORDER BY date_utc"):
        out.append(Alert("cancellation",
                         f"possibly cancelled (not acted on): {r['title']} {r['date_local'] or ''}"
                         f" ({r['townloops_slug']}) — {r['cancel_note']}", True))
    from engine import source_health
    for seed in source_health.flagged(conn):
        out.append(Alert("source-health", f"seed {seed['name']} (#{seed['seed_id']}) "
                                          f"{source_health.LABELS.get(seed['health_status'], seed['health_status'])}: "
                                          f"{seed['health_note']}", False))
    # The latest run that ended — until a later one ends ok.
    last = conn.execute("SELECT id, status, started_at, notes FROM analyst_runs "
                        "WHERE status != 'running' ORDER BY id DESC LIMIT 1").fetchone()
    if last and last[1] in ("failed", "interrupted"):
        out.append(Alert("analyst-run", f"analyst run #{last[0]} {last[1]} (started {last[2]})"
                                        + (f": {last[3]}" if last[3] else "")
                                        + " — the next run picks up its work", False))
    return sorted(out, key=lambda a: not a.needs_you)


def as_dicts(alerts: list[Alert]) -> list[dict]:
    return [asdict(a) for a in alerts]


def _held(conn: sqlite3.Connection) -> list[Alert]:
    """Events held for review: one line per kind, a few titles each."""
    from engine import review_hold
    held = review_hold.held(conn)
    out = []
    for kind in review_hold.KINDS:
        rows = [h for h in held if kind in h["kinds"]]
        if rows:
            names = ", ".join(f"{h['title']} ({h['event_uid'][:8]})" for h in rows[:EXAMPLES])
            out.append(Alert("held", f"{len(rows)} event(s) held — {kind.label}: {names}"
                                     + (" …" if len(rows) > EXAMPLES else "")
                                     + " — `autonomy accept|decline <uid>`", True))
    return out


def _api_freeze() -> Optional[dict]:
    from engine.townloops_api import _freeze_path
    p = Path(_freeze_path())
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return data if data.get("resources") else None
