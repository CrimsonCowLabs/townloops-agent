#!/usr/bin/env python3
"""Cron wrapper: event-find pass over every approved seed.

A multi-seed run (Firecrawl + Codex per seed) outlasts Hermes' cron script
timeout (cron.script_timeout_seconds, default 120s), so the run is detached
into its own session and this wrapper returns immediately (event_reconcile.py
adds a supervisor; event-find records its own crashes). Output goes to state/logs/event-find-<date>.log; the run
summary (or a failure) lands in agent_activity for the dashboard ticker. The
skill's own flock stops a second run from overlapping a slow one."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from engine import db

SKILL = Path("/home/agent/.hermes/skills/business/event-find/find.py")


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    db.migrate()  # a code update must not meet an older schema
    # $PYTHON, not this interpreter: Hermes runs cron scripts with the gateway
    # venv, which lacks the skill deps (feedparser etc.) in the docker image.
    # Same convention as engine.orchestrator._PYTHON.
    python = os.environ.get("PYTHON", sys.executable)
    log_dir = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"event-find-{date.today().isoformat()}.log"
    with open(log, "ab") as fh:
        proc = subprocess.Popen(
            [python, str(SKILL), *sys.argv[1:]],
            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"event-find started (pid {proc.pid}); log: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
