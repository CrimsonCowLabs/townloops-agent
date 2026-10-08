#!/usr/bin/env python3
"""Cron wrapper: daily entity live sync (≤15 per run) for Hermes-profile towns.

Docker towns run sync.py straight from cron/jobs.json's `prompt`; a native
profile's no_agent job can only name a script in its own scripts/ dir, and
Hermes passes it no arguments, so the nightly limit lives here. Fifteen
entities at GET + PATCH + GET each can outlast Hermes' cron script timeout
(default 120s), so the run is detached and this wrapper returns at once.
Output goes to state/logs/entity-live-sync-<date>.log; per-entity outcomes
land in agent_activity."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from engine import db

SKILL = Path("/home/agent/.hermes/skills/business/entity-live-sync/sync.py")
DEFAULT_ARGS = ["--limit", "15"]


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    db.migrate()  # a code update must not meet an older schema
    # $PYTHON, not this interpreter: Hermes runs cron scripts with the gateway
    # venv, which lacks the skill deps. Same convention as event_find.py.
    python = os.environ.get("PYTHON", sys.executable)
    log_dir = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"entity-live-sync-{date.today().isoformat()}.log"
    with open(log, "ab") as fh:
        proc = subprocess.Popen(
            [python, str(SKILL), "run", *(sys.argv[1:] or DEFAULT_ARGS)],
            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"entity-live-sync started (pid {proc.pid}); log: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
