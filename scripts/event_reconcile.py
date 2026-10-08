#!/usr/bin/env python3
"""Cron wrapper: weekly event-reconcile (pull → cross-reference live → stage → enrich).

A full run (every approved seed through Firecrawl + Codex, then Firecrawl
enrichment lookups) takes far longer than Hermes' cron script timeout
(cron.script_timeout_seconds, default 120s), so a supervisor is detached into
its own session and this wrapper returns immediately. The supervisor runs the
skill and waits on it: a run that exits nonzero (token expiry, API or
pagination error, migration error, crash, kill) is reported to the operator as
a `run-failed` row on the dashboard ticker and a Telegram message carrying the
log's last line. Output goes to state/logs/event-reconcile-<date>.log; a
successful run's summary lands in state/reports/reconcile-<date>.md (+
Telegram). The skill's own flock stops a second run from overlapping a slow one."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

SKILL = Path("/home/agent/.hermes/skills/business/event-reconcile/reconcile.py")


def _python() -> str:
    # $PYTHON, not this interpreter: Hermes runs cron scripts with the gateway
    # venv, which lacks the skill deps. Same convention as event_find.py.
    return os.environ.get("PYTHON", sys.executable)


def _log_tail(log: Path, lines: int = 20) -> list[str]:
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.strip()][-lines:]


def report_failure(rc: int, log: Path) -> None:
    """Put a failed run in front of the operator: dashboard ticker + Telegram."""
    tail = _log_tail(log)
    why = f"killed by signal {-rc}" if rc < 0 else f"exit {rc}"
    last = tail[-1][:300] if tail else "(no output)"
    try:
        from engine import inspection
        inspection.record(worker="event-reconcile", action="run-failed", target="run",
                          ok=False, error=f"{why}: {last}", detail=f"log: {log.name}")
    except Exception:  # noqa: BLE001 — still try Telegram
        pass
    try:
        from engine.orchestrator import _telegram_notify
        _telegram_notify(f"[{os.environ.get('TOWNLOOPS_TOWN_SLUG', '?')}] weekly reconcile failed "
                         f"({why}): {last}\nLog: {log.name}")
    except Exception:  # noqa: BLE001 — notification is best-effort
        pass


def supervise(log: Path, args: list[str]) -> int:
    """Run the skill to completion (output appended to `log`) and report a
    nonzero exit."""
    with open(log, "ab") as fh:
        # Unbuffered, so a traceback on stderr lands after the progress lines
        # printed before it and the log's last line is the error.
        rc = subprocess.run([_python(), str(SKILL), "run", *args],
                            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"}).returncode
    if rc != 0:
        report_failure(rc, log)
    return rc


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--supervise"]:
        return supervise(Path(argv[1]), argv[2:])
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    log_dir = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"event-reconcile-{date.today().isoformat()}.log"
    with open(log, "ab") as fh:
        proc = subprocess.Popen(
            [_python(), str(Path(__file__).resolve()), "--supervise", str(log), *argv],
            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"event-reconcile started (pid {proc.pid}); log: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
