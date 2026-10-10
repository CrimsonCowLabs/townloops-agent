#!/usr/bin/env python3
"""Cron wrapper: workqueue drain heartbeat.

Runs every 10 min. For each next_action in the workqueue, claim up to
N items and invoke the matching skill (engine.orchestrator.drain_workqueue).

A drain that reads several detail pages (Firecrawl + Codex each) outlasts
Hermes' cron script timeout (cron.script_timeout_seconds, default 120s), so,
like event_find.py, the drain is detached into its own session and this
wrapper returns immediately. The drain bounds itself (the town's
`workqueue_drain` budget, engine.orchestrator.drain_settings) and holds the
`workqueue-drain` flock, so an overlapping heartbeat bails instead of
colliding; this wrapper doesn't even start one while a drain holds it.
Output goes to state/logs/workqueue-heartbeat-<date>.log.

`--drain` runs the drain in this process (what the detached child runs; also
the standalone entry point for non-cron use).
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from engine import db
from engine import lock

# The drain this wrapper detaches: itself, with --drain.
SKILL = Path(__file__).resolve()


def drain() -> int:
    from engine import orchestrator
    result = orchestrator.drain_workqueue()
    return 0 if not result.get("error") else 1


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--drain"]:
        return drain()
    db.migrate()  # a code update must not meet an older schema
    try:
        with lock.acquire("workqueue-drain"):
            pass
    except lock.LockHeld as e:
        print(f"workqueue drain still running ({e}); not starting another")
        return 0
    # $PYTHON, not this interpreter: Hermes runs cron scripts with the gateway
    # venv, which lacks the skill deps. Same convention as event_find.py.
    python = os.environ.get("PYTHON", sys.executable)
    log_dir = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"workqueue-heartbeat-{date.today().isoformat()}.log"
    with open(log, "ab") as fh:
        proc = subprocess.Popen(
            [python, str(SKILL), "--drain"],
            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"workqueue drain started (pid {proc.pid}); log: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
