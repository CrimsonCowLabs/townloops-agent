"""Workqueue drainer — the heart of the event-first pipeline.

The old phase-machine (bootstrapping → discovering → verifying → …) is
gone. Now: for each next_action, claim N items, invoke the matching
skill, and either mark-complete or mark-failed. Skills that don't exist
yet (M2/M3 milestones haven't landed) are quietly skipped.

Called every 10 min by the `workqueue-heartbeat` cron, and ad hoc when
the operator clicks a "Force …" button on the dashboard.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from engine import db as _db
from engine import event_state
from engine import inspection
from engine import lock as _lock
from engine import workqueue


REPO_ROOT = Path(__file__).resolve().parent.parent
_HERMES_SKILLS = Path(
    os.environ.get("HERMES_HOME", "/home/agent/.hermes")
) / "skills" / "business"
SKILLS_ROOT = _HERMES_SKILLS if _HERMES_SKILLS.is_dir() else REPO_ROOT / "skills" / "business"

_PYTHON = os.environ.get("PYTHON", sys.executable)

# Map workqueue next_action → skill dir + entry file.
# Note: there is no `extract-metadata` action — event-find lands events directly
# at state=metadata-extracted, so the `found → extract-metadata` step is skipped.
_ACTION_SKILLS: dict[str, tuple[str, str]] = {
    "read-detail":       ("event-read-detail",      "read.py"),
    "extract-venue":     ("event-derive-venue",     "derive.py"),
    "extract-hosts":     ("event-derive-hosts",     "derive.py"),
    "extract-people":    ("event-derive-people",    "derive.py"),
    "enrich-entities":   ("event-enrich-entities",  "enrich.py"),
    "ship":              ("event-ship",             "ship.py"),
}

# How many items to claim per action per heartbeat tick
_BATCH_SIZE = 5
# A detail read is a Firecrawl scrape + a Codex call (up to ~100s); one per
# tick keeps it from eating Hermes' 120s cron script budget for the rest.
_BATCH_SIZES = {"read-detail": 1}


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Telegram (best-effort; silent on failure). Kept for phase-transition-esque
# notifications (e.g. "3 events shipped this cycle").
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
# Skill invocation
# --------------------------------------------------------------------------

def _invoke_skill(skill: str, entry: str, event_uid: str,
                  timeout_s: int = 120) -> tuple[int, str, str]:
    """Run a skill's entry script with `--event-uid <uid>`. Returns
    (returncode, stdout_tail, stderr_tail)."""
    path = SKILLS_ROOT / skill / entry
    if not path.exists():
        return (127, "", f"skill entry missing: {path}")
    cmd = [_PYTHON, str(path), "--event-uid", event_uid]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_s, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return (124, "", f"timed out after {timeout_s}s")
    return (proc.returncode, (proc.stdout or "").strip()[-2000:],
            (proc.stderr or "").strip()[-2000:])


def drain_workqueue(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Single heartbeat: for each action, claim up to _BATCH_SIZE items,
    invoke the skill, mark complete or failed. Returns a summary dict."""
    conn = conn or _db.connect()
    conn.row_factory = sqlite3.Row
    # Every town runs this heartbeat on repo code, so it keeps the schema in
    # step with it: a pull no longer needs a manual `town-init setup`.
    _db.run_migrations(conn)
    town = os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")

    try:
        with _lock.acquire("workqueue-drain"):
            # Reclaim any rows orphaned by a drainer that died mid-run before
            # servicing the action loop, so they can't stall invisibly.
            reaped = workqueue.reap_stale_claims(conn)
            per_action: dict[str, dict] = {}
            for action, (skill, entry) in _ACTION_SKILLS.items():
                claims = workqueue.claim(conn, action, limit=_BATCH_SIZES.get(action, _BATCH_SIZE))
                if not claims:
                    continue
                completed = 0
                failed = 0
                skipped = 0
                for c in claims:
                    uid = c["event_uid"]
                    inspection.record(
                        worker="orchestrator", action="skill-invoke",
                        target=uid, detail=f"{skill} for event {uid[:12]}",
                    )
                    rc, out, err = _invoke_skill(skill, entry, uid)
                    if rc == 127:
                        # Skill entry missing (should not happen for the mapped
                        # actions). Treated as a failed attempt: fail() increments
                        # attempts and, at MAX_ATTEMPTS, marks the event stuck-
                        # so it surfaces to the operator rather than looping.
                        workqueue.fail(conn, c["queue_id"],
                                       f"skill entry missing: {skill}")
                        skipped += 1
                        continue
                    if rc == 0:
                        # Skill succeeded — advance event state
                        next_state = event_state.next_state_for(action)
                        current_state = conn.execute(
                            "SELECT state FROM events WHERE event_uid = ?",
                            (uid,),
                        ).fetchone()
                        if current_state and next_state:
                            from_state = current_state["state"]
                            if event_state.attempt_advance(
                                conn, uid, from_state, next_state,
                            ):
                                # If the new state has a next action, enqueue it
                                next_action = event_state.next_action_for(next_state)
                                if next_action:
                                    workqueue.enqueue(conn, uid, next_action)
                        workqueue.complete(conn, c["queue_id"])
                        completed += 1
                    else:
                        workqueue.fail(conn, c["queue_id"],
                                       f"rc={rc}: {err[:400]}")
                        failed += 1
                per_action[action] = {
                    "completed": completed,
                    "failed": failed,
                    "skipped_not_implemented": skipped,
                    "claimed": len(claims),
                }

            status = {
                "town": town,
                "ts": _iso_utc_now(),
                "reaped_stale_claims": reaped,
                "per_action": per_action,
                "any_work": bool(per_action) or bool(reaped),
            }
            print(json.dumps(status, ensure_ascii=False))
            return status
    except _lock.LockHeld:
        status = {"town": town, "error": "workqueue-drain lock held by another tick"}
        print(json.dumps(status))
        return status


# --------------------------------------------------------------------------
# Legacy-compatible entry points — the dashboard's Force-tick button and
# get_status()/summary() calls still work. Force-tick now just drains the
# workqueue.
# --------------------------------------------------------------------------

def run_tick(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Legacy name — the dashboard's Force-tick handler calls this. The
    old phase machine is gone; a tick now means 'drain the workqueue'."""
    return drain_workqueue(conn)


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
    d["workqueue"] = workqueue.counts_by_action(conn)
    return d


def summary(conn: Optional[sqlite3.Connection] = None, hours: int = 24) -> dict:
    """Nightly digest — trimmed for event-first. Counts events by state,
    workqueue depth, recent activity. Writes markdown to state/reports/
    and Telegram-posts a compact summary."""
    conn = conn or _db.connect()
    conn.row_factory = sqlite3.Row
    town = os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    events_by_state = {r["state"]: r["count"] for r in conn.execute(
        "SELECT state, COUNT(*) AS count FROM events GROUP BY state"
    ).fetchall()}
    approvals_pending = conn.execute(
        "SELECT COUNT(*) FROM events WHERE approval_status = 'pending' AND state = 'metadata-extracted'"
    ).fetchone()[0]
    wq = workqueue.counts_by_action(conn)

    seeds = {r["status"]: r["count"] for r in conn.execute(
        "SELECT status, COUNT(*) AS count FROM event_seeds GROUP BY status"
    ).fetchall()}

    md_lines = [
        f"# {town} — {today}",
        f"_Event-first pipeline nightly digest_",
        "",
        f"**Events by state**: " + ", ".join(f"{k}={v}" for k, v in events_by_state.items()) if events_by_state else "**Events by state**: (none)",
        f"**Awaiting approval**: {approvals_pending}",
        f"**Seeds**: " + ", ".join(f"{k}={v}" for k, v in seeds.items()) if seeds else "**Seeds**: none discovered yet",
        "",
        "## Workqueue",
    ]
    if wq:
        for action, counts in wq.items():
            md_lines.append(f"- `{action}`: pending={counts['pending']}, in_flight={counts['in_flight']}, retrying={counts['retrying']}, resolved={counts['resolved']}")
    else:
        md_lines.append("- (empty)")

    report_dir = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{today}.md"
    report_path.write_text("\n".join(md_lines))

    tg = (
        f"[{town}] nightly digest ({today})\n"
        f"events: {sum(events_by_state.values())} across {len(events_by_state)} states\n"
        f"pending approval: {approvals_pending}\n"
        f"seeds: {seeds}\n"
    )
    _telegram_notify(tg)

    return {
        "town": town,
        "report_path": str(report_path),
        "events_by_state": events_by_state,
        "approvals_pending": approvals_pending,
        "seeds": seeds,
        "workqueue": wq,
    }


# --------------------------------------------------------------------------
# Retired: run_tick's old callers expect a `finalize` function that force-
# advances the phase machine. In event-first there is no phase to advance,
# so finalize is a no-op that just returns the current status.
# --------------------------------------------------------------------------

def finalize(conn: Optional[sqlite3.Connection] = None) -> dict:
    return get_status(conn)


PHASES = ("event-first",)  # backward-compat symbol for anything importing it
