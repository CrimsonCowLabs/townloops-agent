"""Subprocess wrappers around skill CLIs.

Direct SQL is fine for `discovery_candidates.status` flips, but anything
that needs the skill's business logic (Listmonk push, source approve,
orchestrator tick, entity-research classify) goes through subprocess so
we don't duplicate logic.

Every helper returns (returncode, stdout_tail, stderr_tail). The route
handler decides whether to flash success or error.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Iterable


_HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes"))
_SKILLS_ROOT = _HERMES_HOME / "skills" / "business"
_SPAWN_LOG_DIR = _HERMES_HOME / "state" / "spawn-logs"

_PYTHON = os.environ.get("PYTHON", "python3")
_DEFAULT_TIMEOUT_S = 60


def _spawn_log_path(skill: str) -> Path:
    """One log file per spawn — dashboard can tail these to debug silent
    failures. Filename embeds ts + PID so concurrent runs don't clobber."""
    _SPAWN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S")
    return _SPAWN_LOG_DIR / f"{skill}-{ts}-{os.getpid()}.log"


def rotate_spawn_logs(older_than_days: int = 7) -> int:
    """Delete spawn log files older than N days.

    Called from the FastAPI startup hook so log volume stays bounded
    without a separate cron. 100 chat turns/day × 7 days ≈ 700 files
    is the target ceiling; anything older is safe to drop.

    Returns the count of files deleted."""
    if not _SPAWN_LOG_DIR.is_dir():
        return 0
    cutoff = time.time() - (older_than_days * 86400)
    n = 0
    for p in _SPAWN_LOG_DIR.iterdir():
        if not p.is_file():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


def _run(argv: list[str], timeout_s: int = _DEFAULT_TIMEOUT_S) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=timeout_s, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return (124, "", f"timed out after {timeout_s}s")
    stdout_tail = (proc.stdout or "").strip()[-2000:]
    stderr_tail = (proc.stderr or "").strip()[-2000:]
    return (proc.returncode, stdout_tail, stderr_tail)


def _skill(skill: str, entry: str, args: Iterable[str],
           timeout_s: int = _DEFAULT_TIMEOUT_S) -> tuple[int, str, str]:
    path = _SKILLS_ROOT / skill / entry
    if not path.exists():
        return (127, "", f"missing skill entry: {path}")
    return _run([_PYTHON, str(path), *args], timeout_s=timeout_s)


def _spawn(argv: list[str], log_path: Path | None = None) -> tuple[bool, str]:
    """Fire-and-forget: kick a subprocess and return immediately.

    stdout+stderr go to `log_path` (a real file), NOT /dev/null — silent
    failure was a real UX problem: skills would die between spawn and
    completion and the operator had no trace to look at. Files under
    state/spawn-logs/ let the dashboard show `tail -n 40 <latest>` when
    someone asks "what happened to my click?"
    """
    log_path = log_path or (_SPAWN_LOG_DIR / f"spawn-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_fh = log_path.open("w")
    except OSError as exc:
        return (False, f"cannot open log {log_path}: {exc}")
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,   # detach so the FastAPI worker's exit doesn't SIGHUP us
        )
    except OSError as exc:
        log_fh.close()
        return (False, f"{type(exc).__name__}: {exc}")
    # Popen dup2's the fd; we can close our handle safely.
    log_fh.close()
    return (True, str(log_path))


def spawn_skill(skill: str, entry: str, args: Iterable[str]) -> tuple[bool, str]:
    path = _SKILLS_ROOT / skill / entry
    if not path.exists():
        return (False, f"missing skill entry: {path}")
    return _spawn([_PYTHON, str(path), *args], log_path=_spawn_log_path(skill))


def spawn_chat_worker(op_message_id: int,
                      placeholder_message_id: int) -> tuple[bool, str]:
    """Fire-and-forget the chat worker for one operator turn. Runs as a
    module inside the already-mounted `dashboard/` package (NOT a bin/
    script — bin/ isn't mounted into the container).

    Returns (ok, log_path_or_error). The caller has already inserted the
    placeholder agent row; the worker will UPDATE it on completion.
    """
    return _spawn(
        [_PYTHON, "-m", "dashboard.chat_worker",
         str(op_message_id), str(placeholder_message_id)],
        log_path=_spawn_log_path("chat"),
    )


# --------------------------------------------------------------------------
# Public wrappers
# --------------------------------------------------------------------------

def orchestrate_tick() -> tuple[int, str, str]:
    return _skill("onboarding-orchestrator", "orchestrate.py", ["tick"],
                  timeout_s=120)


def orchestrate_status() -> tuple[int, str, str]:
    return _skill("onboarding-orchestrator", "orchestrate.py", ["status"])


def orchestrate_summary() -> tuple[int, str, str]:
    return _skill("onboarding-orchestrator", "orchestrate.py", ["summary"])


def sweep(kind: str, per_source: int = 10) -> tuple[int, str, str]:
    if kind not in ("business", "community"):
        return (2, "", f"unknown kind: {kind}")
    return _skill(f"{kind}-discovery", "discover.py",
                  ["sweep", "--per-source", str(per_source)],
                  timeout_s=180)


def harvest_events(limit: int = 20) -> tuple[int, str, str]:
    """Walk promoted entities to find event feeds. Dashboard-safe limit
    keeps a real button click under the 300s timeout (20 entities * ~3
    probes at 1rps ≈ 60-90s)."""
    return _skill("event-source-harvest", "harvest.py",
                  ["--limit", str(limit)],
                  timeout_s=300)


def newsletter_show(week: str) -> tuple[int, str, str]:
    return _skill("newsletter-weekly", "newsletter.py",
                  ["show", "--week", week])


def newsletter_push(week: str) -> tuple[int, str, str]:
    return _skill("newsletter-weekly", "newsletter.py",
                  ["push", "--week", week, "--yes-really-send"],
                  timeout_s=120)


def source_approve(host: str) -> tuple[int, str, str]:
    return _skill("event-source-discovery", "discover.py",
                  ["approve", host])


def source_list_json() -> tuple[int, str, str]:
    # `discover.py list` already prints one JSON object per line —
    # no --json flag needed. Parse line-by-line.
    return _skill("event-source-discovery", "discover.py", ["list"])
