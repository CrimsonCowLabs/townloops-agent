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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import requests

from engine import autonomy
from engine import db as _db
from engine.derive_helpers import load_town_profile
from engine import event_detail, event_groups
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

# How many items to claim per action per drain
_BATCH_SIZE = 5
# Longest one skill run may take (_invoke_skill kills it after this).
SKILL_TIMEOUT_S = 120
# A detail read is a Firecrawl scrape + a Codex call (up to ~100s). The
# heartbeat runs detached (scripts/workqueue_heartbeat.py), outside Hermes'
# 120s cron script budget, so a drain may fetch several — up to the town's
# `workqueue_drain.read_detail_per_drain` and only in the first half of the
# drain's budget, so the rest of the pipeline always gets the other half.
# Events with no detail page of their own finish at once and don't count
# (engine/event_detail.needs_fetch). The hourly workqueue_rate_limits still
# bound every claim.
_FETCHING_ACTIONS = {"read-detail"}
MAX_READ_DETAIL_PER_DRAIN = 20
# A drain stops starting skills once its budget is spent; the last one may run
# SKILL_TIMEOUT_S more. The whole drain must end before workqueue.
# reap_stale_claims (15 min) would take its claims, with a skill run to spare.
_MIN_BUDGET_S = 60
_MAX_BUDGET_S = 15 * 60 - 2 * SKILL_TIMEOUT_S
# A skill's "not now" (autonomous submissions held): the claim is released
# without counting an attempt.
RC_DEFERRED = 75


@dataclass(frozen=True)
class DrainSettings:
    # Defaults: half the budget (210s) fits a handful of typical detail reads
    # (~30–60s), the cap bounds the fast case; 420s + one skill run ends
    # before the next 10-min heartbeat. Was 1 fetch per tick in-budget.
    read_detail_per_drain: int = 6
    budget_seconds: int = 420


def drain_settings(profile: Optional[dict] = None) -> DrainSettings:
    """The town's `workqueue_drain` block (town-profile.yaml), read fresh each
    drain. Missing or bad values keep the defaults; all are clamped to safe
    bounds."""
    profile = load_town_profile() if profile is None else (profile or {})
    src = profile.get("workqueue_drain") or {}
    d = DrainSettings()

    def num(key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(src[key]) if src.get(key) is not None else default
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    return DrainSettings(
        read_detail_per_drain=num("read_detail_per_drain", d.read_detail_per_drain,
                                  1, MAX_READ_DETAIL_PER_DRAIN),
        budget_seconds=num("budget_seconds", d.budget_seconds, _MIN_BUDGET_S, _MAX_BUDGET_S),
    )


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
                  timeout_s: int = SKILL_TIMEOUT_S) -> tuple[int, str, str]:
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


def drain_workqueue(conn: Optional[sqlite3.Connection] = None, *,
                    clock: Callable[[], float] = time.monotonic) -> dict:
    """Single heartbeat: for each action, claim up to _BATCH_SIZE items,
    invoke the skill, mark complete or failed. No skill starts once the
    drain's budget (drain_settings) is spent; claims it can't get to go back
    to the queue unattempted. Returns a summary dict."""
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
            mode = autonomy.settings()
            drain = drain_settings()
            start = clock()
            budget_spent = False
            resolved = resolve_stuck(conn, mode) if mode.autonomous else {}
            per_action: dict[str, dict] = {}
            for action, (skill, entry) in _ACTION_SKILLS.items():
                if budget_spent:
                    break
                if action == "ship" and mode.autonomous:
                    gate = autonomy.submission_gate(conn, mode)
                    if gate.verdict != "submit":
                        per_action[action] = {"held": gate.reason}
                        continue
                fetching_action = action in _FETCHING_ACTIONS
                claims = workqueue.claim(
                    conn, action,
                    limit=max(drain.read_detail_per_drain, _BATCH_SIZE)
                    if fetching_action else _BATCH_SIZE)
                if not claims:
                    continue
                completed = 0
                failed = 0
                skipped = 0
                deferred = 0
                fetching = 0
                for c in claims:
                    uid = c["event_uid"]
                    elapsed = clock() - start
                    budget_spent = elapsed >= drain.budget_seconds
                    hold = budget_spent
                    if not hold and fetching_action and event_detail.needs_fetch(conn, c):
                        hold = (fetching >= drain.read_detail_per_drain
                                or elapsed >= drain.budget_seconds / 2)
                        fetching += not hold
                    if hold:   # next drain, no attempt counted
                        workqueue.release(conn, c["queue_id"])
                        deferred += 1
                        continue
                    inspection.record(
                        worker="orchestrator", action="skill-invoke",
                        target=uid, detail=f"{skill} for event {uid[:12]}",
                    )
                    rc, out, err = _invoke_skill(skill, entry, uid)
                    if rc == RC_DEFERRED:
                        workqueue.release(conn, c["queue_id"])
                        skipped += 1
                        continue
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
                    "deferred": deferred,
                    "claimed": len(claims),
                }

            status = {
                "town": town,
                "ts": _iso_utc_now(),
                "reaped_stale_claims": reaped,
                "elapsed_s": round(clock() - start, 1),
                "budget_spent": budget_spent,
                **({"stuck_resolved": resolved} if resolved else {}),
                "per_action": per_action,
                "any_work": bool(per_action) or bool(reaped),
            }
            print(json.dumps(status, ensure_ascii=False))
            return status
    except _lock.LockHeld:
        status = {"town": town, "error": "workqueue-drain lock held by another tick"}
        print(json.dumps(status))
        return status


def resolve_stuck(conn: sqlite3.Connection, mode: autonomy.Settings,
                  now: Optional[datetime] = None) -> dict:
    """Autonomous: settle every `stuck-` event by policy instead of waiting
    for an operator — retry it after a backoff, ship it with the fields it
    has, or drop it with a reason (engine/autonomy.resolve_stuck)."""
    counts: dict[str, int] = {}
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE state LIKE 'stuck-%' ORDER BY state_updated_at").fetchall()]
    for ev in rows:
        d = autonomy.resolve_stuck(ev, mode, now)
        uid, state = ev["event_uid"], ev["state"]
        if d.verdict == "wait":
            continue
        if d.verdict == "retry":
            restored = event_state.unstuck(conn, uid)
            action = event_state.next_action_for(restored or "")
            if action:
                workqueue.enqueue(conn, uid, action)
                conn.execute("UPDATE event_workqueue SET attempts = 0 "
                             "WHERE event_uid = ? AND next_action = ?", (uid, action))
            extra = autonomy.extra(ev)
            extra["autonomy"] = {**(extra.get("autonomy") or {}),
                                 "retries": d.evidence["retries"]}
            conn.execute("UPDATE events SET extra_json = ? WHERE event_uid = ?",
                         (json.dumps(extra), uid))
        elif d.verdict == "ship-as-is":
            if event_state.attempt_advance(conn, uid, state, "entities-enriched"):
                workqueue.enqueue(conn, uid, "ship")
                conn.execute("UPDATE event_workqueue SET attempts = 0 "
                             "WHERE event_uid = ? AND next_action = 'ship'", (uid,))
        elif d.verdict == "drop":
            event_state.decline(conn, uid, f"[autonomy] {d.reason}", from_states=(state,))
        else:
            continue
        autonomy.record(conn, "stuck", uid, d)
        counts[d.verdict] = counts.get(d.verdict, 0) + 1
    conn.commit()
    return counts


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

    # Shipped in the window, one line per Series (event_groups), not per date.
    cutoff = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    shipped_rows = [dict(r) for r in conn.execute(
        "SELECT event_uid, COALESCE(title, name) AS title, COALESCE(date_utc, date) AS date_sort, "
        "COALESCE(date_local, date_utc, date) AS date_display, series_slug "
        "FROM events WHERE state = 'shipped' AND state_updated_at >= ?", (cutoff,)
    ).fetchall()]
    shipped = event_groups.group_local_rows(shipped_rows)
    shipped_counts = {"events": len(shipped_rows), **event_groups.split_counts(shipped)}

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

    md_lines += ["", f"## Shipped (last {hours}h)"]
    for g in shipped:
        r = g.representative
        if g.is_series:
            md_lines.append(f"- {r['title']} — Series `{g.series_slug}`, {len(g.events)} "
                            f"date{'s' if len(g.events) != 1 else ''} from {r['date_display']}")
        else:
            md_lines.append(f"- {r['title']} — {r['date_display']}")
    if not shipped:
        md_lines.append("- (none)")

    report_dir = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    mode = autonomy.settings()
    auto = None
    if mode.autonomous:
        from engine import moderation
        auto = moderation.digest(conn, cutoff, _series_report(report_dir))
        md_lines += _autonomy_markdown(auto)
    report_path = report_dir / f"{today}.md"
    report_path.write_text("\n".join(md_lines))

    tg = (
        f"[{town}] nightly digest ({today})\n"
        f"events: {sum(events_by_state.values())} across {len(events_by_state)} states\n"
        + (f"pending approval: {approvals_pending}\n" if not auto else "")
        + f"shipped: {shipped_counts['events']} events ({shipped_counts['series']} Series, "
        f"{shipped_counts['singletons']} one-off)\n"
        f"seeds: {seeds}\n"
    )
    if auto:
        tg += _autonomy_telegram(auto)
    _telegram_notify(tg)

    return {
        "town": town,
        "report_path": str(report_path),
        "events_by_state": events_by_state,
        "approvals_pending": approvals_pending,
        "seeds": seeds,
        "shipped": shipped_counts,
        "workqueue": wq,
        **({"autonomy": auto} if auto else {}),
    }


def _series_report(report_dir: Path) -> Optional[dict]:
    """The latest series-candidates run (skills/business/series-candidates)."""
    try:
        return json.loads((report_dir / "series-candidates-latest.json").read_text())
    except (OSError, ValueError):
        return None


def _autonomy_markdown(a: dict) -> list[str]:
    m = a["moderation"]
    rate = lambda v: "n/a" if v is None else f"{v:.0%}"  # noqa: E731
    lines = ["", "## Autonomous decisions (last 24h)",
             f"- Events submitted to TownLoops: {a['events_submitted']} "
             f"(entities: {a['entities_submitted']}); still pending in TownLoops: "
             f"{a['pending_in_townloops']}",
             f"- Events approved by the agent: {a['events_approved']}; dropped: "
             f"{a['events_dropped']}"
             + (" — " + ", ".join(f"{r} ×{n}" for r, n in a["top_drop_reasons"])
                if a["top_drop_reasons"] else ""),
             f"- Moderator: {m['published']} published, {m['merged']} merged, "
             f"{m['rejected']} rejected (approval {rate(m['approval_rate'])}, "
             f"rejection {rate(m['rejection_rate'])})",
             f"- Seeds added: {', '.join(a['seeds_added']) or 'none'}; retired: "
             f"{', '.join(a['seeds_retired']) or 'none'}",
             f"- Organisations approved for submission: {a['organisations_approved']}"]
    lines += ["", "## Series candidates"]
    lines += [f"- **{c.get('title')}** — {c.get('dates')} dates at "
              f"`{c.get('venue_slug') or c.get('venue_id')}`" for c in a["series_candidates"]] \
        or ["- (none in the latest weekly report)"]
    if a["suspect_series"]:
        lines += [f"- Suspect Series `{d.get('slug')}`: {d.get('venues')} venues"
                  + (f", same-day duplicates {', '.join(d.get('dates') or [])}"
                     if d.get("dates") else "") for d in a["suspect_series"]]
    lines += ["", "## Needs attention"] + ([f"- {x}" for x in a["needs_attention"]]
                                          or ["- nothing"])
    return lines


def _autonomy_telegram(a: dict) -> str:
    m = a["moderation"]
    out = (f"submitted: {a['events_submitted']} events, {a['entities_submitted']} entities "
           f"({a['pending_in_townloops']} pending in TownLoops)\n"
           f"dropped: {a['events_dropped']}"
           + (" — top: " + ", ".join(f"{r} ×{n}" for r, n in a["top_drop_reasons"][:3])
              if a["top_drop_reasons"] else "") + "\n"
           f"moderator: {m['published'] + m['merged']} accepted, {m['rejected']} rejected\n"
           f"seeds: +{len(a['seeds_added'])} added, {len(a['seeds_retired'])} retired\n")
    if a["series_candidates"]:
        out += f"Series candidates: {len(a['series_candidates'])}\n"
    if a["needs_attention"]:
        out += "needs attention:\n" + "\n".join(f"• {x}" for x in a["needs_attention"][:8]) + "\n"
    return out


# --------------------------------------------------------------------------
# Retired: run_tick's old callers expect a `finalize` function that force-
# advances the phase machine. In event-first there is no phase to advance,
# so finalize is a no-op that just returns the current status.
# --------------------------------------------------------------------------

def finalize(conn: Optional[sqlite3.Connection] = None) -> dict:
    return get_status(conn)


PHASES = ("event-first",)  # backward-compat symbol for anything importing it
