"""Slash-command router for the operator chat panel — dashboard-local
fast path, no LLM.

Every command in this module completes in <1s: pure SQL reads via
`db_v2` or fire-and-forget spawns via `skills.spawn_skill`. Natural-
language messages (anything not starting with `/`) are routed
elsewhere (chat_worker → Hermes api_server; see M6).

The parser is deliberately minimal — regex, not argparse — so operator
typos surface as "unknown syntax" rather than argparse's noisy usage
dumps. Each handler returns a plain-text agent reply body.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Callable, Optional

from dashboard import chat_db, db_v2, skills


HELP_TEXT = """Available commands (all instant, no LLM):
  /help                           show this
  /status                         event / seed / workqueue counts
  /pending [N]                    list N pending events (default 5)
  /stuck                          workqueue rows with attempts >= 3
  /seeds proposed|approved|rejected
                                  list seeds by status
  /discover-seeds                 kick event-seed-discovery in background
  /find-events [--seed-id N]      kick event-find in background

Ask anything else in plain English — I'll route it to the Hermes agent.
Destructive actions (approve, decline) go through the agent so they get
a two-turn confirmation."""


def is_slash(text: str) -> bool:
    return bool(text) and text.lstrip().startswith("/")


def handle_slash(text: str) -> str:
    """Route a slash message to the matching handler. Returns the agent
    reply body. Unknown commands return a friendly hint, not an error."""
    raw = text.strip()
    if not raw.startswith("/"):
        return "Not a slash command."
    parts = shlex.split(raw[1:]) if raw != "/" else []
    if not parts:
        return HELP_TEXT
    cmd = parts[0].lower()
    args = parts[1:]
    handler = _COMMANDS.get(cmd)
    if not handler:
        return (f"Unknown command `/{cmd}`. Try `/help`.")
    try:
        return handler(args)
    except Exception as exc:  # noqa: BLE001 — surface as a chat msg not a 500
        return f"`/{cmd}` failed: {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Individual handlers
# --------------------------------------------------------------------------

def _cmd_help(_: list[str]) -> str:
    return HELP_TEXT


def _cmd_status(_: list[str]) -> str:
    chips = db_v2.headline_chips()
    return (
        "Status:\n"
        f"  seeds approved:   {chips['seeds_approved']}\n"
        f"  events pending:   {chips['pending']}\n"
        f"  events in-flight: {chips['in_flight']}\n"
        f"  events shipped:   {chips['shipped']}\n"
        f"  workqueue active: {chips['workqueue_active']}"
        + (f"  · {chips['workqueue_stuck']} STUCK" if chips['workqueue_stuck'] else "")
    )


def _cmd_pending(args: list[str]) -> str:
    n = 5
    if args:
        try:
            n = max(1, min(50, int(args[0])))
        except ValueError:
            return f"Usage: `/pending [N]` — N must be an integer, got `{args[0]}`."
    events = db_v2.events_by_approval("pending", limit=n)
    if not events:
        return "No pending events."
    lines = [f"Pending events (showing {len(events)}):"]
    for e in events:
        when = (e.get("date_display") or "?")[:19]
        title = (e.get("title") or "(untitled)")[:70]
        lines.append(f"  · {when} — {title}")
    return "\n".join(lines)


def _cmd_stuck(_: list[str]) -> str:
    rows = db_v2.workqueue_stuck()
    if not rows:
        return "No stuck workqueue items."
    lines = [f"Stuck items ({len(rows)}):"]
    for r in rows:
        title = (r.get("event_title") or r.get("event_uid", "?")[:12])
        err = (r.get("last_error") or "").split("\n")[0][:60]
        lines.append(f"  · queue_id={r['queue_id']} action={r['next_action']} "
                     f"attempts={r['attempts']} — {title}: {err}")
    return "\n".join(lines)


def _cmd_seeds(args: list[str]) -> str:
    status = args[0].lower() if args else "approved"
    if status not in ("proposed", "approved", "rejected"):
        return "Usage: `/seeds proposed|approved|rejected`"
    seeds = db_v2.event_seeds_by_status(status)
    if not seeds:
        return f"No {status} seeds."
    lines = [f"{status.capitalize()} seeds ({len(seeds)}):"]
    for s in seeds:
        last = s.get("last_event_count")
        last_str = f" · last: {last} events" if last is not None else ""
        lines.append(f"  · #{s['seed_id']:2d} [{s['kind']}] {s['name']}{last_str}")
    return "\n".join(lines)


def _cmd_discover_seeds(_: list[str]) -> str:
    ok, msg = skills.spawn_skill(
        "event-seed-discovery", "discover.py", ["discover", "--limit", "15"],
    )
    if not ok:
        return f"Failed to start seed discovery: {msg}"
    log_name = os.path.basename(msg) if msg else "(no log)"
    return (
        "Seed discovery started (~2 min). "
        "Watch Live Activity for the `✓ done` summary row. "
        f"Log: {log_name}"
    )


_SEED_ID_RE = re.compile(r"^--seed-id[= ](\d+)$|^--seed-id$")


def _cmd_find_events(args: list[str]) -> str:
    """`/find-events` or `/find-events --seed-id N`."""
    skill_args: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--seed-id" and i + 1 < len(args):
            try:
                seed_id = int(args[i + 1])
            except ValueError:
                return f"`--seed-id` needs an integer, got `{args[i + 1]}`."
            skill_args += ["--seed-id", str(seed_id)]
            i += 2
            continue
        if a.startswith("--seed-id="):
            try:
                seed_id = int(a.split("=", 1)[1])
            except ValueError:
                return f"`--seed-id=` needs an integer, got `{a.split('=', 1)[1]}`."
            skill_args += ["--seed-id", str(seed_id)]
            i += 1
            continue
        return f"Unknown arg `{a}`. Usage: `/find-events [--seed-id N]`."
    ok, msg = skills.spawn_skill("event-find", "find.py", skill_args)
    if not ok:
        return f"Failed to start event-find: {msg}"
    log_name = os.path.basename(msg) if msg else "(no log)"
    target = f"seed {skill_args[1]}" if skill_args else "all approved seeds"
    return (
        f"event-find started for {target}. "
        "Watch Live Activity; new events land pending your approval. "
        f"Log: {log_name}"
    )


_COMMANDS: dict[str, Callable[[list[str]], str]] = {
    "help":            _cmd_help,
    "status":          _cmd_status,
    "pending":         _cmd_pending,
    "stuck":           _cmd_stuck,
    "seeds":           _cmd_seeds,
    "discover-seeds":  _cmd_discover_seeds,
    "find-events":     _cmd_find_events,
}
