"""Source health: notice when an established seed suddenly stops listing
events — a redesigned page, a moved calendar, a broken widget — instead of
the town quietly losing a source.

    (fine) ──latest run 0, or < (100-drop_pct)% of the trailing average──▶ zero | drop
      ▲                                                                       │
      └──── count back to ≥ (100-drop_pct)% of the baseline, or operator ack ─┘

Every event-find run of a seed goes through `record_run` (one `source_runs`
row, `fetched` = events the run listed). Only successful runs are judged —
a failed scrape says nothing about the source's calendar. A seed needs
`min_runs` successful runs behind it before it's judged; the trailing
average is over the last `window` of them. Zero alerts on any seed that
averaged at least one event; a drop only on seeds averaging `min_baseline`+.

An alert is raised once and held (`event_seeds.health_status`) against the
average it dropped from (`health_baseline`), so a lasting drop doesn't clear
itself as the history catches up and doesn't re-alert every run. `ack`
accepts the seed's new level: earlier runs stop counting, so the seed isn't
judged again until `min_runs` more successful runs make up its new baseline.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from engine.derive_helpers import dict_rows as _rows, iso_utc_now, seed_ref

LABELS = {"zero": "listed nothing", "drop": "sharp drop", "recovered": "recovered"}
DEFAULTS = {"window": 6, "min_runs": 3, "drop_pct": 50, "min_baseline": 4}


def settings_from(profile: dict) -> dict:
    """`source_health:` in town-profile.yaml over the defaults."""
    return {**DEFAULTS, **((profile or {}).get("source_health") or {})}


def record_run(conn: sqlite3.Connection, seed: dict, *, started: str, ended: str,
               ok: bool, fetched: int, inserted: int = 0, updated: int = 0,
               error: Optional[str] = None, settings: Optional[dict] = None,
               judge: bool = True) -> Optional[dict]:
    """Record one event-find run of `seed` and (unless `judge` is off, as for
    an extraction that failed to parse) judge the seed's health. Returns {seed, seed_id, status:
    zero|drop|recovered, note} when it changed, else None."""
    source = seed_ref(seed["name"])
    conn.execute(
        "INSERT INTO source_runs (source, started_at, ended_at, ok, fetched, inserted, updated, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source, started, ended, 1 if ok else 0, fetched, inserted, updated, error))
    if not (ok and judge):
        return None
    cfg = settings or DEFAULTS
    state = conn.execute(
        "SELECT health_status, health_baseline, health_since FROM event_seeds WHERE seed_id = ?",
        (seed["seed_id"],)).fetchone()
    if state is None:
        return None
    status, baseline, since = state[0], state[1], state[2]
    keep = 1 - cfg["drop_pct"] / 100

    if status:
        if fetched > 0 and fetched >= baseline * keep:
            _clear(conn, seed["seed_id"])
            return _change(seed, "recovered",
                           f"listed {fetched} events again (was averaging {baseline:.1f})")
        return None

    history = [r[0] for r in conn.execute(
        "SELECT fetched FROM source_runs WHERE source = ? AND ok = 1 AND started_at > ? "
        "ORDER BY started_at DESC, id DESC LIMIT -1 OFFSET 1",
        (source, since or "")).fetchall()][:cfg["window"]]
    if len(history) < cfg["min_runs"]:
        return None
    avg = sum(history) / len(history)
    if fetched == 0 and avg >= 1:
        status = "zero"
    elif avg >= cfg["min_baseline"] and fetched < avg * keep:
        status = "drop"
    else:
        return None
    note = (f"listed {fetched} events (trailing average {avg:.1f} over "
            f"{len(history)} runs)")
    conn.execute("UPDATE event_seeds SET health_status = ?, health_note = ?, health_baseline = ?, "
                 "  health_flagged_at = ? WHERE seed_id = ?",
                 (status, note, avg, iso_utc_now(), seed["seed_id"]))
    return _change(seed, status, note)


def flagged(conn: sqlite3.Connection) -> list[dict]:
    """Seeds with a standing alert, most recent first."""
    return _rows(conn, "SELECT * FROM event_seeds WHERE health_status IS NOT NULL "
                       "ORDER BY health_flagged_at DESC, seed_id")


def ack(conn: sqlite3.Connection, seed_id: int) -> bool:
    """The operator accepts the seed's current level: the alert clears and
    only runs from now on make up its new baseline."""
    row = conn.execute("SELECT name FROM event_seeds WHERE seed_id = ?", (seed_id,)).fetchone()
    if row is None:
        return False
    last = conn.execute("SELECT MAX(started_at) FROM source_runs WHERE source = ?",
                        (seed_ref(row[0]),)).fetchone()[0]
    _clear(conn, seed_id)
    conn.execute("UPDATE event_seeds SET health_since = ? WHERE seed_id = ?",
                 (last or iso_utc_now(), seed_id))
    conn.commit()
    return True


def alert_text(town: str, changes: list[dict]) -> str:
    lines = [f"[{town}] source health:"]
    for c in changes:
        lines.append(f"• {c['seed']} — {LABELS[c['status']]}: {c['note']}")
    return "\n".join(lines)


def _clear(conn: sqlite3.Connection, seed_id: int) -> None:
    conn.execute("UPDATE event_seeds SET health_status = NULL, health_note = NULL, "
                 "  health_baseline = NULL, health_flagged_at = NULL WHERE seed_id = ?",
                 (seed_id,))


def _change(seed: dict, status: str, note: str) -> dict:
    return {"seed": seed["name"], "seed_id": seed["seed_id"], "status": status, "note": note}
