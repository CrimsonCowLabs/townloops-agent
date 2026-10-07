"""Thin sqlite3 read helpers for the dashboard.

Uses the same DB the agent writes to (state/townloops.db). All reads use
WAL mode (already enabled by engine/db.py on connect) so they don't block
concurrent agent writes.

The dashboard's writes are limited to `discovery_candidates.status` flips
(approve/reject). Everything else goes through subprocess → skill CLI so
we don't duplicate business logic here.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


_DB_PATH = Path(
    os.environ.get(
        "TOWNLOOPS_DB_PATH",
        os.path.join(
            os.environ.get("HERMES_HOME", "/home/agent/.hermes"),
            "state", "townloops.db",
        ),
    )
)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Read helpers — used by the overview / candidates / entities pages.
# --------------------------------------------------------------------------

def agent_state() -> dict[str, Any]:
    with connect() as c:
        row = c.execute("SELECT * FROM agent_state WHERE id = 1").fetchone()
    return dict(row) if row else {"phase": "uninitialized"}


def candidate_counts() -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT kind, status, COUNT(*) AS n "
            "FROM discovery_candidates "
            "GROUP BY kind, status "
            "ORDER BY kind, status"
        ).fetchall()
    return [dict(r) for r in rows]


def entity_counts() -> list[dict]:
    out = []
    with connect() as c:
        for kind, table in (("business", "businesses"), ("venue", "venues"),
                            ("community", "communities"),
                            ("performer", "performers"),
                            ("creator", "creators")):
            n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            shipped = c.execute(
                f"SELECT COUNT(*) FROM {table} WHERE pushed_at IS NOT NULL"
            ).fetchone()[0]
            out.append({"kind": kind, "table": table,
                        "total": n, "shipped": shipped})
    return out


def event_counts() -> dict:
    """Events are the customer-visible deliverable — front-and-center on
    the overview. Also breaks down by upcoming vs past so the operator
    can see whether the calendar is stale."""
    with connect() as c:
        total = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        shipped = c.execute(
            "SELECT COUNT(*) FROM events WHERE pushed_at IS NOT NULL"
        ).fetchone()[0]
        upcoming = c.execute(
            "SELECT COUNT(*) FROM events WHERE date_day >= date('now')"
        ).fetchone()[0]
    return {"total": total, "shipped": shipped, "upcoming": upcoming}


def last_tick_summary() -> dict:
    """Parse `agent_state.notes` (JSON blob written by orchestrator on
    each tick) so the overview can show what the last tick actually did."""
    with connect() as c:
        row = c.execute(
            "SELECT phase, tick_count, last_tick_at, notes FROM agent_state WHERE id = 1"
        ).fetchone()
    if not row:
        return {}
    import json as _json
    notes = {}
    try:
        notes = _json.loads(row["notes"] or "{}")
    except _json.JSONDecodeError:
        notes = {"unparseable": True}
    return {
        "phase": row["phase"],
        "tick_count": row["tick_count"],
        "last_tick_at": row["last_tick_at"],
        "notes": notes,
    }


def orchestrator_running() -> bool:
    """True if the orchestrator lock is actually held (some process has
    fcntl.LOCK_EX on it right now). File EXISTENCE alone is not enough —
    the lock file persists after container restarts, so relying on
    existence produces false positives forever after any hard-kill.

    Uses fcntl.flock probe: try to grab the lock non-blocking; if we can
    acquire it, no one else was holding it, so we release immediately and
    return False. If we fail with EWOULDBLOCK, someone holds it → True.
    """
    from pathlib import Path
    import os, fcntl
    p = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "locks" / "orchestrator.lock"
    if not p.exists():
        return False
    try:
        fd = os.open(p, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Grabbed it — nobody was holding it. Release immediately.
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def next_cron_fires(limit: int = 8) -> list[dict]:
    """Read cron/jobs.json (bind-mounted read-only) and return the next N
    upcoming fires, sorted by next_run_at."""
    from pathlib import Path
    import os, json as _json
    p = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "cron" / "jobs.json"
    if not p.exists():
        return []
    try:
        doc = _json.loads(p.read_text())
    except (OSError, _json.JSONDecodeError):
        return []
    jobs = doc.get("jobs") or []
    upcoming = [
        {
            "id": j.get("id"),
            "name": j.get("name"),
            "next_run_at": j.get("next_run_at"),
            "last_run_at": j.get("last_run_at"),
            "last_status": j.get("last_status"),
            "last_error": (j.get("last_error") or "")[:200] or None,
        }
        for j in jobs
        if j.get("next_run_at")
    ]
    upcoming.sort(key=lambda x: x["next_run_at"])
    return upcoming[:limit]


def _classify_cron_output(path) -> str:
    """Sniff a cron output file for error markers so the Activity page can
    show status per-row. Returns 'ok', 'error', 'partial', or 'empty'.

    Distinction:
      - 'error':   the outer subprocess itself failed. The whole tick /
                   sweep didn't get to do its work. Exit code non-zero,
                   Traceback at top level, or `handler_error` in the
                   orchestrator summary.
      - 'partial': the outer subprocess succeeded (phase may have even
                   advanced) but one or more inner sub-skills reported
                   errors (e.g. optimistic-fanout on kinds that don't have
                   a CLI yet). Not an operator emergency.
      - 'ok':      clean run, no error markers at all.
      - 'empty':   0-byte file (Hermes had nothing to write).

    Pre-fix behavior: any `"error":` substring anywhere → 'error'. That
    made expected sub-skill misses (community-enrichment / creator-enrichment
    with no enrich.py yet) drown out actual outer failures.
    """
    try:
        st = path.stat()
    except OSError:
        return "empty"
    if st.st_size == 0:
        return "empty"
    try:
        # Read the tail — orchestrator summary is the last JSON line.
        with path.open("rb") as f:
            f.seek(max(0, st.st_size - 8192))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return "empty"
    lower = chunk.lower()

    # Outer failures — the tick / cron subprocess didn't complete cleanly.
    outer_error_markers = (
        'traceback (most recent call last)',
        'modulenotfounderror',
        'handler_error',
        '**exit code:** 1',
        '**exit code:** 2',
        '**exit code:** 3',
        '**status:** failed',
    )
    if any(m in lower for m in outer_error_markers):
        return "error"

    # Inner sub-skill errors — outer succeeded, but summary carries an
    # `"error":` entry from a fanned-out subprocess. Still useful signal
    # (operator may want to investigate) but doesn't block the loop.
    inner_error_markers = (
        '"error":',
        '"returncode": 1',
        '"returncode": 2',
        '"returncode": 3',
        '"failed"',
    )
    if any(m in lower for m in inner_error_markers):
        return "partial"

    return "ok"


def cron_output_files(job_id: str | None = None, limit: int = 40) -> list[dict]:
    """List cron output markdown files under cron/output/<job_id>/, tagged
    with a rough status (ok / error / empty) sniffed from the body."""
    from pathlib import Path
    import os
    root = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "cron" / "output"
    if not root.exists():
        return []
    entries = []
    dirs = [root / job_id] if job_id else [d for d in root.iterdir() if d.is_dir()]
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir(), reverse=True):
            if f.suffix != ".md":
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            entries.append({
                "job_id": d.name,
                "filename": f.name,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "status": _classify_cron_output(f),
            })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries[:limit]


def cron_output_read(job_id: str, filename: str) -> str | None:
    """Read a specific cron output markdown file. Returns None if missing
    or path escapes the cron/output/ tree."""
    from pathlib import Path
    import os
    if "/" in filename or ".." in filename or "/" in job_id or ".." in job_id:
        return None
    root = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "cron" / "output"
    p = (root / job_id / filename).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    if not p.exists() or not p.is_file():
        return None
    try:
        return p.read_text(errors="replace")
    except OSError:
        return None


def orchestrator_stuck(min_ago: int = 90) -> bool:
    """True if agent_state.last_tick_at is more than `min_ago` minutes old
    (or never set). Fires the amber alert on the live-strip and Branch 1 of
    the empty-state cascade — usually the operator's real problem when
    events aren't landing."""
    with connect() as c:
        row = c.execute(
            "SELECT last_tick_at, "
            "  CAST((julianday('now') - julianday(last_tick_at)) * 24 * 60 AS INTEGER) AS mins_ago "
            "FROM agent_state WHERE id = 1"
        ).fetchone()
    if not row or not row["last_tick_at"]:
        return True  # never ticked = stuck
    return (row["mins_ago"] or 0) >= min_ago


def pending_approvals_count() -> dict:
    """Two chips for the status strip: `candidates` waiting for verify,
    `sources` waiting for operator approval on the Sources page."""
    with connect() as c:
        cand = c.execute(
            "SELECT COUNT(*) FROM discovery_candidates WHERE status = 'new'"
        ).fetchone()[0]
    src = 0
    import os as _os
    from pathlib import Path as _Path
    disc = _Path(_os.environ.get(
        "DISCOVERY_DB_PATH",
        _os.path.join(_os.environ.get("HERMES_HOME", "/home/agent/.hermes"),
                      "state", "discovery.db"),
    ))
    if disc.exists():
        c2 = sqlite3.connect(disc)
        try:
            src = c2.execute(
                "SELECT COUNT(*) FROM candidates WHERE status = 'needs_review'"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        c2.close()
    return {"candidates": cand, "sources": src}


# --------------------------------------------------------------------------
# Events — the product. Everything below powers Band 2 and Band 3.
# --------------------------------------------------------------------------

def _urlparse_host(url: str) -> str:
    from urllib.parse import urlparse
    try:
        h = (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def _source_labels() -> dict:
    """Map source name → (human-readable name, host). Reads sources.yaml
    so the ticker/lists can show 'Kings Beach Chamber Calendar' instead of
    'sf.eater.com'."""
    import os as _os
    from pathlib import Path as _Path
    import yaml
    p = _Path(_os.environ.get(
        "SOURCES_YAML_PATH",
        _os.path.join(_os.environ.get("HERMES_HOME", "/home/agent/.hermes"),
                      "state", "sources.yaml"),
    ))
    labels = {}
    if not p.exists():
        return labels
    try:
        doc = yaml.safe_load(p.read_text()) or {}
    except Exception:
        return labels
    for s in doc.get("sources") or []:
        name = s.get("name") or ""
        if name:
            labels[name] = {
                "label": name,
                "host": _urlparse_host(s.get("url") or ""),
                "parser": s.get("parser"),
                "url": s.get("url"),
            }
    return labels


def events_upcoming(window_days: int = 30, limit: int = 200,
                    include_hidden: bool = False) -> list[dict]:
    """Ordered by date ascending. Adds source_label + source_host from
    sources.yaml when possible."""
    where = ["date_day >= date('now')",
             f"date_day <= date('now', '+{int(window_days)} days')"]
    if not include_hidden:
        where.append("hidden = 0")
    with connect() as c:
        rows = c.execute(
            f"""
            SELECT event_uid, name, date, date_day, end_at, venue_name,
                   location, hosting_group, source, source_url,
                   category_slug, description, pushed_at, hidden
            FROM events
            WHERE {' AND '.join(where)}
            ORDER BY date ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    labels = _source_labels()
    out = []
    for r in rows:
        d = dict(r)
        sl = labels.get(d["source"] or "") or {}
        d["source_label"] = sl.get("label") or d["source"] or "—"
        d["source_host"] = sl.get("host") or _urlparse_host(d.get("source_url") or "")
        out.append(d)
    return out


def events_by_day(days: int = 30) -> list[dict]:
    """[{day: 'YYYY-MM-DD', count: n}] for the calendar heatmap. Fills in
    zero-count days so the grid stays dense."""
    with connect() as c:
        rows = c.execute(
            f"""
            SELECT date_day AS day, COUNT(*) AS count
            FROM events
            WHERE hidden = 0
              AND date_day >= date('now')
              AND date_day <= date('now', '+{int(days)} days')
            GROUP BY date_day
            """
        ).fetchall()
    counts = {r["day"]: r["count"] for r in rows}
    from datetime import date, timedelta
    today = date.today()
    out = []
    for i in range(days + 1):
        d = (today + timedelta(days=i)).isoformat()
        out.append({"day": d, "count": counts.get(d, 0)})
    return out


def events_by_source(days: int = 30, limit: int = 10) -> list[dict]:
    """Top event-producing sources over the window. Joins with source_runs
    for last-fetched timestamps."""
    with connect() as c:
        rows = c.execute(
            f"""
            SELECT
              e.source,
              COUNT(*) AS event_count,
              MAX(e.first_seen_at) AS latest_ingested,
              (SELECT MAX(started_at) FROM source_runs sr WHERE sr.source = e.source) AS last_fetched,
              (SELECT COUNT(*) FROM source_runs sr WHERE sr.source = e.source AND ok = 1) AS ok_runs,
              (SELECT COUNT(*) FROM source_runs sr WHERE sr.source = e.source) AS total_runs
            FROM events e
            WHERE hidden = 0
              AND e.first_seen_at >= datetime('now', '-{int(days)} days')
            GROUP BY e.source
            ORDER BY event_count DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    labels = _source_labels()
    out = []
    for r in rows:
        d = dict(r)
        sl = labels.get(d["source"] or "") or {}
        d["source_label"] = sl.get("label") or d["source"] or "—"
        d["source_host"] = sl.get("host") or ""
        out.append(d)
    return out


def silent_sources(days: int = 30) -> list[dict]:
    """Approved sources that fetched but produced 0 events in the window.
    Labeled 'quiet' in-UI (may be seasonally empty, not necessarily broken)."""
    labels = _source_labels()
    if not labels:
        return []
    with connect() as c:
        # Which approved sources have produced events in the window?
        rows_producing = {
            r["source"] for r in c.execute(
                f"""
                SELECT DISTINCT source FROM events
                WHERE first_seen_at >= datetime('now', '-{int(days)} days')
                """
            ).fetchall()
        }
        # For each source in sources.yaml, count runs + last-run.
        out = []
        for name, meta in labels.items():
            if name in rows_producing:
                continue
            run_row = c.execute(
                "SELECT MAX(started_at) AS last, COUNT(*) AS n "
                "FROM source_runs WHERE source = ?",
                (name,),
            ).fetchone()
            out.append({
                "source_label": meta["label"],
                "source_host": meta["host"],
                "parser": meta["parser"],
                "url": meta["url"],
                "last_fetched": run_row["last"] if run_row else None,
                "run_count": run_row["n"] if run_row else 0,
            })
    return sorted(out, key=lambda x: (x["last_fetched"] or ""), reverse=True)


def venues_without_events(limit: int = 20) -> list[dict]:
    """Coverage-estimate helper (heuristic — see plan). Rows in venues +
    venue-ish communities with no matching hosting_group OR venue_name in
    the last 30 days of events."""
    with connect() as c:
        rows = c.execute(
            """
            SELECT 'venue' AS kind, entity_uid, name, website, source_url,
                   category_slug
            FROM venues
            WHERE (website IS NOT NULL OR source_url IS NOT NULL)
              AND entity_uid NOT IN (
                SELECT DISTINCT v.entity_uid FROM venues v
                JOIN events e ON (e.hosting_group = v.name OR e.venue_name = v.name)
                WHERE e.first_seen_at >= datetime('now', '-30 days')
              )
            UNION ALL
            SELECT 'community' AS kind, entity_uid, name, website, source_url,
                   category_slug
            FROM communities
            WHERE (website IS NOT NULL OR source_url IS NOT NULL)
              AND entity_uid NOT IN (
                SELECT DISTINCT c.entity_uid FROM communities c
                JOIN events e ON (e.hosting_group = c.name OR e.venue_name = c.name)
                WHERE e.first_seen_at >= datetime('now', '-30 days')
              )
            ORDER BY name
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def events_recent_ingested(limit: int = 20) -> list[dict]:
    """The activity ticker: last N events by first_seen_at (when we
    discovered them, not when they happen)."""
    with connect() as c:
        rows = c.execute(
            f"""
            SELECT event_uid, name, date, date_day, venue_name,
                   hosting_group, source, source_url, first_seen_at
            FROM events
            WHERE hidden = 0
            ORDER BY first_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    labels = _source_labels()
    out = []
    for r in rows:
        d = dict(r)
        sl = labels.get(d["source"] or "") or {}
        d["source_label"] = sl.get("label") or d["source"] or "—"
        d["source_host"] = sl.get("host") or _urlparse_host(d.get("source_url") or "")
        out.append(d)
    return out


def event_by_uid(uid: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM events WHERE event_uid = ?", (uid,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    labels = _source_labels()
    sl = labels.get(d["source"] or "") or {}
    d["source_label"] = sl.get("label") or d["source"] or "—"
    d["source_host"] = sl.get("host") or _urlparse_host(d.get("source_url") or "")
    return d


def set_event_hidden(uid: str, hidden: bool) -> int:
    with connect() as c:
        cur = c.execute(
            "UPDATE events SET hidden = ? WHERE event_uid = ?",
            (1 if hidden else 0, uid),
        )
    return cur.rowcount


# --------------------------------------------------------------------------
# Sources (for the refetch action + coverage panels)
# --------------------------------------------------------------------------

def source_by_name(name: str) -> dict | None:
    return _source_labels().get(name)


def source_counts() -> dict:
    """discovery.db candidate counts by status (proposed / needs_review /
    approved / rejected). Uses a separate db from the state DB."""
    import os
    from pathlib import Path
    disc_path = Path(os.environ.get(
        "DISCOVERY_DB_PATH",
        os.path.join(os.environ.get("HERMES_HOME", "/home/agent/.hermes"),
                     "state", "discovery.db"),
    ))
    if not disc_path.exists():
        return {"proposed": 0, "needs_review": 0, "approved": 0, "rejected": 0}
    conn = sqlite3.connect(disc_path)
    conn.row_factory = sqlite3.Row
    out = {"proposed": 0, "needs_review": 0, "approved": 0, "rejected": 0}
    try:
        for r in conn.execute("SELECT status, COUNT(*) n FROM candidates GROUP BY status"):
            if r["status"] in out:
                out[r["status"]] = r["n"]
    except sqlite3.OperationalError:
        pass  # table doesn't exist yet
    conn.close()
    return out


def list_candidates(kind: Optional[str], status: Optional[str],
                    page: int = 0, page_size: int = 50) -> list[dict]:
    where = []
    params: list[Any] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if status:
        where.append("status = ?")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with connect() as c:
        rows = c.execute(
            f"""
            SELECT candidate_uid, kind, status, canonical_url,
                   display_name, source_signal, first_seen_at,
                   target_category_slug, reject_reason, live_slug
            FROM discovery_candidates
            {where_sql}
            ORDER BY first_seen_at DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, page * page_size),
        ).fetchall()
    return [dict(r) for r in rows]


def get_candidate(uid: str) -> Optional[dict]:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM discovery_candidates WHERE candidate_uid = ?",
            (uid,),
        ).fetchone()
    return dict(row) if row else None


def list_entities(kind: str, limit: int = 100) -> list[dict]:
    tables = {
        "business": "businesses", "venue": "venues",
        "community": "communities", "performer": "performers",
        "creator": "creators",
    }
    table = tables.get(kind)
    if not table:
        return []
    with connect() as c:
        rows = c.execute(
            f"""
            SELECT entity_uid, name, category_slug, source_url,
                   pushed_at, first_seen_at
            FROM {table}
            ORDER BY first_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_source_runs(hours: int = 24) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            """
            SELECT source, COUNT(*) AS runs,
                   SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok,
                   SUM(COALESCE(fetched, 0))  AS fetched,
                   SUM(COALESCE(inserted, 0)) AS inserted,
                   SUM(COALESCE(updated, 0))  AS updated,
                   MAX(error) AS sample_error
            FROM source_runs
            WHERE started_at >= datetime('now', ?)
            GROUP BY source
            ORDER BY inserted DESC, updated DESC
            """,
            (f'-{hours} hours',),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Mutations — status flips only. Skill CLIs handle everything else.
# --------------------------------------------------------------------------

def approve_candidate(uid: str) -> int:
    """Flip status to 'verified'. Skips the Firecrawl verify — trusts the operator.
    Returns rows affected."""
    with connect() as c:
        cur = c.execute(
            "UPDATE discovery_candidates "
            "SET status = 'verified', last_seen_at = ? "
            "WHERE candidate_uid = ? AND status IN ('new', 'rejected')",
            (_iso_utc_now(), uid),
        )
    return cur.rowcount


def reject_candidate(uid: str, reason: str = "operator") -> int:
    with connect() as c:
        cur = c.execute(
            "UPDATE discovery_candidates "
            "SET status = 'rejected', reject_reason = ?, last_seen_at = ? "
            "WHERE candidate_uid = ?",
            (reason, _iso_utc_now(), uid),
        )
    return cur.rowcount


def bulk_approve(kind: Optional[str], source_signal_like: Optional[str]) -> int:
    where = ["status = 'new'"]
    params: list[Any] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if source_signal_like:
        where.append("source_signal LIKE ?")
        params.append(f"%{source_signal_like}%")
    with connect() as c:
        cur = c.execute(
            f"""
            UPDATE discovery_candidates
            SET status = 'verified', last_seen_at = ?
            WHERE {" AND ".join(where)}
            """,
            (_iso_utc_now(), *params),
        )
    return cur.rowcount


def sources_grouped_by_signal() -> list[dict]:
    """For the bulk-approve UI: group new candidates by source_signal so
    the operator sees 'these 12 came from directory:gotahoenorth.com'."""
    with connect() as c:
        rows = c.execute(
            """
            SELECT source_signal, kind, COUNT(*) AS n
            FROM discovery_candidates
            WHERE status = 'new'
            GROUP BY source_signal, kind
            ORDER BY n DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]
