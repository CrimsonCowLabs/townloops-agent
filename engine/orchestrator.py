"""Onboarding orchestrator — the per-town autonomous loop.

Called once per cron fire (hourly during bootstrap; the tick is a no-op
after `steady_state`). Each tick:

  1. Acquire engine.lock.acquire("orchestrator") to serialize.
  2. Load agent_state (init if missing).
  3. Force-advance if phase_ticks >= PHASE_MAX_TICKS (168 = ~7 days hourly).
  4. Dispatch to the current phase handler. Handler shells out to skill
     CLIs, returns (advance, quiet, summary).
  5. Update agent_state (tick_count, phase_ticks, quiet_ticks, last_tick_at,
     notes) — transactional.
  6. On advance: transition, reset phase state.
  7. Emit one-line JSON status to stdout.
  8. On phase transition, send a Telegram summary if TELEGRAM_BOT_TOKEN + CHAT_ID set.

Phase handlers are dumb shellouts — they don't touch agent_state directly.
That keeps the machine's state transitions in one place (this file) and
the phase work in the skill CLIs (grep-friendly per skill).
"""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import requests

from engine import db as _db
from engine import lock as _lock


PHASES = (
    "bootstrapping",
    "discovering",
    "verifying",
    "researching",
    "enriching",
    "shipping",
    "steady_state",
)

PHASE_MAX_TICKS = 168  # ~7 days at hourly cadence
QUIET_TICKS_TO_ADVANCE = 3


REPO_ROOT = Path(__file__).resolve().parent.parent
# Per-town containers only bind-mount `engine/` under /app; the actual
# skills live at $HERMES_HOME/skills/business (bind-mounted separately).
# Fall back to the repo layout for out-of-container invocations (tests,
# SLT bundled compose).
_HERMES_SKILLS = Path(
    os.environ.get("HERMES_HOME", "/home/agent/.hermes")
) / "skills" / "business"
SKILLS_ROOT = _HERMES_SKILLS if _HERMES_SKILLS.is_dir() else REPO_ROOT / "skills" / "business"


# --------------------------------------------------------------------------
# State I/O
# --------------------------------------------------------------------------

def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_state_row(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM agent_state WHERE id = 1").fetchone()
    if row:
        return dict(row)
    conn.execute(
        """
        INSERT INTO agent_state (id, phase, entered_at, tick_count,
                                 phase_ticks, quiet_ticks, notes)
        VALUES (1, 'bootstrapping', ?, 0, 0, 0, '{}')
        """,
        (_iso_utc_now(),),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM agent_state WHERE id = 1").fetchone()
    return dict(row)


def _write_tick(conn: sqlite3.Connection, *, phase: str, phase_ticks: int,
                quiet_ticks: int, tick_count: int, notes: dict,
                advanced_to: Optional[str] = None,
                entered_at: Optional[str] = None) -> None:
    if advanced_to:
        conn.execute(
            """
            UPDATE agent_state
            SET phase = ?, entered_at = ?, last_tick_at = ?,
                tick_count = ?, phase_ticks = 0, quiet_ticks = 0,
                notes = ?
            WHERE id = 1
            """,
            (advanced_to, entered_at or _iso_utc_now(), _iso_utc_now(),
             tick_count, json.dumps(notes)),
        )
    else:
        conn.execute(
            """
            UPDATE agent_state
            SET last_tick_at = ?, tick_count = ?,
                phase_ticks = ?, quiet_ticks = ?, notes = ?
            WHERE id = 1
            """,
            (_iso_utc_now(), tick_count, phase_ticks, quiet_ticks,
             json.dumps(notes)),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Shell helpers
# --------------------------------------------------------------------------

_PYTHON = os.environ.get("PYTHON", sys.executable)


def _skill_run(skill: str, entry: str, argv: list[str]) -> dict:
    """Run a skill CLI and parse its last JSON line as the summary.

    Returns a dict — if the skill prints multiple JSON lines, only the last
    is used (matches the one-line status convention). On non-zero exit,
    returns {"error": stderr, "returncode": N}.
    """
    path = SKILLS_ROOT / skill / entry
    if not path.exists():
        return {"error": f"missing skill entry: {path}"}
    cmd = [_PYTHON, str(path), *argv]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=900,
    )
    tail_json: Optional[dict] = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            tail_json = json.loads(line)
        except json.JSONDecodeError:
            continue
    if proc.returncode != 0:
        return {
            "error": (proc.stderr or "").strip()[-500:] or "non-zero exit",
            "returncode": proc.returncode,
            "summary": tail_json,
        }
    return tail_json or {"summary": (proc.stdout or "").strip()[-200:]}


# --------------------------------------------------------------------------
# Phase handlers — each returns (advance: bool, quiet: bool, summary: dict)
# --------------------------------------------------------------------------

def _phase_bootstrapping(conn: sqlite3.Connection, notes: dict) -> tuple[bool, bool, dict]:
    # If the town has been populated by some other pipeline already, skip
    # onboarding entirely. This protects existing deployments (SLT) that
    # gain the new orchestrator on redeploy from restarting discovery.
    existing = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM businesses UNION ALL SELECT 1 FROM venues LIMIT 1)"
    ).fetchone()[0]
    if existing:
        # Emit summary and fast-forward to steady_state via run_tick's advance chain.
        # We can't advance more than one phase per tick, so callers should re-run
        # ticks until the phase settles — the `run_tick` machinery handles that.
        notes["skip_onboarding"] = "existing entities present — advancing straight to steady_state"
        conn.execute(
            "UPDATE agent_state SET phase = 'steady_state', entered_at = ?, "
            "phase_ticks = 0, quiet_ticks = 0 WHERE id = 1",
            (_iso_utc_now(),),
        )
        conn.commit()
        return (False, False, {"skipped": "town already populated", "existing": bool(existing)})

    result = _skill_run("town-init", "init.py", ["setup"])
    return (True, False, {"town_init": result})


def _phase_discovering(conn: sqlite3.Connection, notes: dict) -> tuple[bool, bool, dict]:
    biz = _skill_run("business-discovery",  "discover.py", ["sweep", "--per-source", "25"])
    com = _skill_run("community-discovery", "discover.py", ["sweep", "--per-source", "25"])

    biz_new = int(biz.get("inserted_new") or 0) if isinstance(biz, dict) else 0
    com_new = int(com.get("inserted_new") or 0) if isinstance(com, dict) else 0
    net_new = biz_new + com_new

    quiet = (net_new == 0)
    # Advance decision handled by run_tick based on quiet_ticks counter.
    return (False, quiet, {"business": biz, "community": com, "net_new": net_new})


def _phase_verifying(conn: sqlite3.Connection, notes: dict) -> tuple[bool, bool, dict]:
    biz = _skill_run("business-discovery",  "discover.py", ["verify", "--limit", "20"])
    com = _skill_run("community-discovery", "discover.py", ["verify", "--limit", "20"])

    # Advance when verified backlog is small AND `new` backlog is empty.
    new_backlog = conn.execute(
        "SELECT COUNT(*) FROM discovery_candidates WHERE status = 'new'"
    ).fetchone()[0]
    verified_backlog = conn.execute(
        "SELECT COUNT(*) FROM discovery_candidates WHERE status = 'verified'"
    ).fetchone()[0]

    biz_proc = int(biz.get("processed") or 0) if isinstance(biz, dict) else 0
    com_proc = int(com.get("processed") or 0) if isinstance(com, dict) else 0
    processed = biz_proc + com_proc

    advance = (new_backlog == 0 and verified_backlog < 5)
    quiet = (processed == 0)
    return (advance, quiet, {
        "business": biz, "community": com,
        "new_backlog": new_backlog, "verified_backlog": verified_backlog,
    })


def _phase_researching(conn: sqlite3.Connection, notes: dict) -> tuple[bool, bool, dict]:
    # First: promote verified candidates whose confidence bar was met.
    biz_promote = _skill_run("business-discovery",  "discover.py", ["promote", "--limit", "10"])
    com_promote = _skill_run("community-discovery", "discover.py", ["promote", "--limit", "10"])

    # Then: classify any entity rows still missing category_slug.
    classify_out: dict[str, dict] = {}
    for kind in ("business", "community", "venue", "performer", "creator"):
        r = _skill_run("entity-research", "research.py",
                       ["classify", "--kind", kind, "--limit", "10"])
        classify_out[kind] = r

    verified_backlog = conn.execute(
        "SELECT COUNT(*) FROM discovery_candidates WHERE status = 'verified'"
    ).fetchone()[0]

    processed = sum(
        int((r.get("processed") if isinstance(r, dict) else 0) or 0)
        for r in list(classify_out.values()) + [biz_promote, com_promote]
    )

    advance = (verified_backlog == 0)
    quiet = (processed == 0)
    return (advance, quiet, {
        "promote_business": biz_promote,
        "promote_community": com_promote,
        "classify": classify_out,
        "verified_backlog": verified_backlog,
    })


def _phase_enriching(conn: sqlite3.Connection, notes: dict) -> tuple[bool, bool, dict]:
    out: dict[str, dict] = {}
    for kind_skill in ("venue-enrichment", "business-enrichment", "community-enrichment",
                       "performer-enrichment", "creator-enrichment"):
        # Not every enrichment skill is expected to exist yet — SPECS was just
        # extended, community/creator wrappers land in a follow-up. Missing
        # skill CLIs just get a benign error entry.
        r = _skill_run(kind_skill, "enrich.py",
                       ["sweep", "--limit", "20", "--firecrawl"])
        out[kind_skill] = r

    unresolved = conn.execute(
        "SELECT COUNT(*) FROM enrichment_queue "
        "WHERE claimed_at IS NULL AND resolved_at IS NULL"
    ).fetchone()[0]

    processed = sum(
        int((r.get("processed") if isinstance(r, dict) else 0) or 0)
        for r in out.values()
    )

    advance = (unresolved == 0)
    quiet = (processed == 0)
    return (advance, quiet, {"enrich": out, "unresolved_queue": unresolved})


def _phase_shipping(conn: sqlite3.Connection, notes: dict) -> tuple[bool, bool, dict]:
    ship = _skill_run("event-ingest", "ingest.py", ["ship"])

    unshipped = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT entity_uid FROM businesses  WHERE pushed_at IS NULL
          UNION ALL
          SELECT entity_uid FROM venues      WHERE pushed_at IS NULL
          UNION ALL
          SELECT entity_uid FROM communities WHERE pushed_at IS NULL
          UNION ALL
          SELECT entity_uid FROM performers  WHERE pushed_at IS NULL
          UNION ALL
          SELECT entity_uid FROM creators    WHERE pushed_at IS NULL
        )
        """
    ).fetchone()[0]

    # quiet = nothing shipped this tick
    shipped_here = 0
    if isinstance(ship, dict):
        for k, v in ship.items():
            if isinstance(v, dict) and "shipped" in v:
                try:
                    shipped_here += int(v["shipped"])
                except (TypeError, ValueError):
                    pass

    quiet = (shipped_here == 0)
    advance = False  # advance handled by run_tick after QUIET_TICKS_TO_ADVANCE quiet ticks
    return (advance, quiet, {"ship": ship, "unshipped_entities": unshipped,
                              "shipped_here": shipped_here})


def _phase_steady_state(conn: sqlite3.Connection, notes: dict) -> tuple[bool, bool, dict]:
    return (False, True, {"noop": True})


_HANDLERS: dict[str, Callable] = {
    "bootstrapping": _phase_bootstrapping,
    "discovering":   _phase_discovering,
    "verifying":     _phase_verifying,
    "researching":   _phase_researching,
    "enriching":     _phase_enriching,
    "shipping":      _phase_shipping,
    "steady_state":  _phase_steady_state,
}


# --------------------------------------------------------------------------
# Telegram (best-effort; silent on failure)
# --------------------------------------------------------------------------

def _telegram_notify(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text},
            timeout=10,
        )
    except requests.RequestException:
        pass


# --------------------------------------------------------------------------
# LLM health transitions — fire Telegram once per healthy↔throttled flip.
# Health is written by skills/business/entity-research/research.py after
# each codex-exec call. The last-notified status persists in the same file
# so we don't spam on every tick while throttled.
# --------------------------------------------------------------------------

_HERMES_HOME = os.environ.get("HERMES_HOME", "/home/agent/.hermes")
_LLM_HEALTH_PATH = Path(_HERMES_HOME) / "state" / "llm_health.json"


def _notify_llm_transition(town: str) -> None:
    if not _LLM_HEALTH_PATH.exists():
        return
    try:
        payload = json.loads(_LLM_HEALTH_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return

    status = payload.get("status")
    last_notified = payload.get("last_notified_status")
    if not status or status == last_notified:
        return

    if status == "throttled":
        err = (payload.get("last_error") or "")[:120]
        msg = f"[{town}] LLM throttled — {err}"
    elif status == "healthy":
        # If we were previously throttled/unavailable, celebrate recovery.
        if last_notified in ("throttled", "unavailable"):
            msg = f"[{town}] LLM recovered — classifier back online"
        else:
            # First-ever healthy write; skip the notification but still
            # update last_notified so we don't announce it later.
            msg = None
    elif status == "unavailable":
        msg = f"[{town}] LLM unavailable — {(payload.get('last_error') or '')[:120]}"
    else:
        msg = None

    if msg:
        _telegram_notify(msg)

    payload["last_notified_status"] = status
    payload["last_notified_at"] = _iso_utc_now()
    try:
        _LLM_HEALTH_PATH.write_text(json.dumps(payload, indent=2))
    except OSError:
        pass


# --------------------------------------------------------------------------
# Public entry
# --------------------------------------------------------------------------

def _next_phase(current: str) -> str:
    idx = PHASES.index(current)
    return PHASES[min(idx + 1, len(PHASES) - 1)]


def run_tick(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Run one orchestrator tick. Returns the status dict (also printed as JSON)."""
    conn = conn or _db.connect()
    conn.row_factory = sqlite3.Row
    town = os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")

    # Fire a Telegram once per LLM healthy↔throttled flip. Reads whatever
    # entity-research last recorded; safe to call when the file is missing.
    _notify_llm_transition(town)

    try:
        with _lock.acquire("orchestrator"):
            state = ensure_state_row(conn)
            phase = state["phase"]
            phase_ticks = int(state["phase_ticks"] or 0) + 1
            quiet_ticks = int(state["quiet_ticks"] or 0)
            tick_count = int(state["tick_count"] or 0) + 1

            forced = False
            if phase_ticks >= PHASE_MAX_TICKS and phase not in ("steady_state",):
                forced = True

            handler = _HANDLERS.get(phase, _phase_steady_state)
            try:
                advance, quiet, summary = handler(conn, {})
            except Exception as exc:
                summary = {"handler_error": f"{type(exc).__name__}: {exc}"}
                advance = False
                quiet = True

            if quiet:
                quiet_ticks += 1
            else:
                quiet_ticks = 0

            # Phases that advance on N consecutive quiet ticks (rather than
            # a hard predicate).
            if phase in ("discovering", "shipping") and quiet_ticks >= QUIET_TICKS_TO_ADVANCE:
                advance = True

            if forced:
                advance = True

            advanced_to = None
            if advance and phase != "steady_state":
                advanced_to = _next_phase(phase)

            _write_tick(
                conn,
                phase=phase,
                phase_ticks=phase_ticks,
                quiet_ticks=quiet_ticks,
                tick_count=tick_count,
                notes=summary,
                advanced_to=advanced_to,
            )

            status = {
                "town": town,
                "phase": advanced_to or phase,
                "prev_phase": phase if advanced_to else None,
                "phase_ticks": 0 if advanced_to else phase_ticks,
                "quiet_ticks": 0 if advanced_to else quiet_ticks,
                "tick_count": tick_count,
                "forced_advance": forced and bool(advanced_to),
                "summary": summary,
            }
            print(json.dumps(status, ensure_ascii=False))

            if advanced_to:
                msg = f"[{town}] phase {phase} → {advanced_to}"
                if forced:
                    msg = f"[{town}] phase {phase} stuck — force-advancing to {advanced_to}"
                _telegram_notify(msg)

            return status
    except _lock.LockHeld:
        status = {"town": town, "error": "orchestrator lock held by another tick"}
        print(json.dumps(status))
        return status


def get_status(conn: Optional[sqlite3.Connection] = None) -> dict:
    conn = conn or _db.connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM agent_state WHERE id = 1").fetchone()
    if not row:
        return {"phase": "uninitialized"}
    d = dict(row)
    try:
        d["notes_parsed"] = json.loads(d.get("notes") or "{}")
    except json.JSONDecodeError:
        d["notes_parsed"] = None
    return d


def summary(conn: Optional[sqlite3.Connection] = None,
            hours: int = 24) -> dict:
    """Nightly rollup: sources scraped, rows added/updated per kind,
    enrichment queue depth, LLM health. Writes a markdown report to
    state/reports/YYYY-MM-DD.md and Telegrams a compact digest.

    Returns the summary dict (also printed as JSON by the CLI wrapper).
    """
    conn = conn or _db.connect()
    conn.row_factory = sqlite3.Row
    town = os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")
    now = datetime.now(timezone.utc)
    since_iso = (now - timedelta(hours=hours)).isoformat(timespec="seconds")

    # source_runs — one row per source per fetch.
    src_rows = conn.execute(
        """
        SELECT source,
               COUNT(*) AS runs,
               SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_runs,
               SUM(COALESCE(fetched, 0))  AS fetched,
               SUM(COALESCE(inserted, 0)) AS inserted,
               SUM(COALESCE(updated, 0))  AS updated,
               SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) AS errored,
               MAX(error) AS sample_error
        FROM source_runs
        WHERE started_at >= ?
        GROUP BY source
        ORDER BY inserted DESC, updated DESC
        """,
        (since_iso,),
    ).fetchall()

    # post_counters — per-kind POSTs shipped to TownLoops today.
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    post_rows = conn.execute(
        """
        SELECT day, resource, count
        FROM post_counters
        WHERE day >= ?
        ORDER BY day DESC, resource
        """,
        (yesterday,),
    ).fetchall()

    # enrichment_queue — how much work is outstanding.
    enq = conn.execute(
        """
        SELECT
          SUM(CASE WHEN claimed_at IS NULL AND resolved_at IS NULL THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN claimed_at IS NOT NULL AND resolved_at IS NULL THEN 1 ELSE 0 END) AS in_flight,
          SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolved
        FROM enrichment_queue
        """
    ).fetchone()

    # LLM health snapshot.
    llm_health: dict = {}
    if _LLM_HEALTH_PATH.exists():
        try:
            llm_health = json.loads(_LLM_HEALTH_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            llm_health = {"status": "unknown", "read_error": True}

    # Phase (steady-state check).
    phase_row = conn.execute(
        "SELECT phase, tick_count, phase_ticks, entered_at FROM agent_state WHERE id = 1"
    ).fetchone()
    phase = dict(phase_row) if phase_row else {"phase": "uninitialized"}

    # Build markdown report.
    md_lines: list[str] = []
    md_lines.append(f"# {town} — {today}\n")
    md_lines.append(f"_Generated {now.isoformat(timespec='seconds')} covering last {hours}h_\n")
    md_lines.append("")
    md_lines.append(f"**Phase**: `{phase.get('phase')}`  (tick {phase.get('tick_count')}, "
                    f"phase_ticks {phase.get('phase_ticks')}, entered {phase.get('entered_at')})")
    md_lines.append("")
    md_lines.append(f"**LLM**: `{llm_health.get('status', 'unknown')}`  "
                    f"(last check {llm_health.get('checked_at', '?')})")
    if llm_health.get("last_error"):
        md_lines.append(f"- last error: `{llm_health['last_error'][:200]}`")
    md_lines.append("")
    md_lines.append("**Enrichment queue**: "
                    f"pending={enq['pending'] or 0}, "
                    f"in_flight={enq['in_flight'] or 0}, "
                    f"resolved={enq['resolved'] or 0}")
    md_lines.append("")

    md_lines.append("## Source runs (last 24h)")
    md_lines.append("")
    if src_rows:
        md_lines.append("| source | runs | ok | fetched | ins | upd | err |")
        md_lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for r in src_rows:
            md_lines.append(
                f"| `{r['source']}` "
                f"| {r['runs']} | {r['ok_runs'] or 0} "
                f"| {r['fetched'] or 0} | {r['inserted'] or 0} "
                f"| {r['updated'] or 0} | {r['errored'] or 0} |"
            )
        md_lines.append("")
        errored = [r for r in src_rows if (r['errored'] or 0) > 0 and r['sample_error']]
        if errored:
            md_lines.append("### Sample errors")
            for r in errored[:5]:
                md_lines.append(f"- `{r['source']}`: {(r['sample_error'] or '')[:200]}")
            md_lines.append("")
    else:
        md_lines.append("_No source runs in window._")
        md_lines.append("")

    md_lines.append("## POSTs shipped (today + yesterday)")
    md_lines.append("")
    if post_rows:
        md_lines.append("| day | resource | count |")
        md_lines.append("|---|---|---:|")
        for r in post_rows:
            md_lines.append(f"| {r['day']} | {r['resource']} | {r['count']} |")
        md_lines.append("")
    else:
        md_lines.append("_Nothing shipped in the last two days._")
        md_lines.append("")

    report_body = "\n".join(md_lines)

    # Write report file.
    reports_dir = Path(_HERMES_HOME) / "state" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{today}.md"
    report_path.write_text(report_body)

    # Compact Telegram summary — fits in a single message.
    tg_lines: list[str] = [
        f"[{town}] nightly digest ({today})",
        f"phase={phase.get('phase')} tick={phase.get('tick_count')}",
        f"llm={llm_health.get('status', 'unknown')}",
    ]
    src_total_ins = sum((r['inserted'] or 0) for r in src_rows)
    src_total_upd = sum((r['updated'] or 0) for r in src_rows)
    src_total_err = sum((r['errored'] or 0) for r in src_rows)
    tg_lines.append(
        f"sources: {len(src_rows)} active, +{src_total_ins} new, "
        f"~{src_total_upd} upd, {src_total_err} err"
    )
    tg_lines.append(
        f"queue: {enq['pending'] or 0}p / {enq['in_flight'] or 0}f / {enq['resolved'] or 0}r"
    )
    posts_today = [r for r in post_rows if r['day'] == today]
    if posts_today:
        tg_lines.append("shipped today: " + ", ".join(
            f"{r['resource']}={r['count']}" for r in posts_today
        ))
    _telegram_notify("\n".join(tg_lines))

    result = {
        "town": town,
        "generated_at": now.isoformat(timespec="seconds"),
        "report_path": str(report_path),
        "phase": phase.get("phase"),
        "llm_status": llm_health.get("status"),
        "sources": len(src_rows),
        "totals": {
            "inserted": src_total_ins,
            "updated": src_total_upd,
            "errored": src_total_err,
        },
        "queue": {
            "pending": enq['pending'] or 0,
            "in_flight": enq['in_flight'] or 0,
            "resolved": enq['resolved'] or 0,
        },
        "posts_today": [dict(r) for r in posts_today],
    }
    return result


def finalize(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Force phase to steady_state — testing / recovery helper."""
    conn = conn or _db.connect()
    ensure_state_row(conn)
    conn.execute(
        "UPDATE agent_state SET phase = 'steady_state', entered_at = ?, "
        "phase_ticks = 0, quiet_ticks = 0 WHERE id = 1",
        (_iso_utc_now(),),
    )
    conn.commit()
    return get_status(conn)
