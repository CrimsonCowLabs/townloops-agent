#!/usr/bin/env python3
"""Cron wrapper: promote operator-approved business candidates (≤3 per run,
every 15 min).

Enriching three businesses before shipping them can outlast Hermes' cron
script timeout (cron.script_timeout_seconds, default 120s), so the run is
detached into its own session and this wrapper returns immediately. Output
goes to state/logs/business-promote-<date>.log; each run that promotes
something lands in agent_activity. With nothing approved the run exits
without touching TownLoops. The skill's own flock stops a second run from
overlapping a slow one (or a sweep)."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

SKILL = Path("/home/agent/.hermes/skills/business/business-discovery/discover.py")


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    # $PYTHON, not this interpreter: Hermes runs cron scripts with the gateway
    # venv, which lacks the skill deps. Same convention as event_find.py.
    python = os.environ.get("PYTHON", sys.executable)
    log_dir = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"business-promote-{date.today().isoformat()}.log"
    with open(log, "ab") as fh:
        proc = subprocess.Popen(
            [python, str(SKILL), "promote", *sys.argv[1:]],
            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"business-discovery promote started (pid {proc.pid}); log: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
