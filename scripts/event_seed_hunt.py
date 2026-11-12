#!/usr/bin/env python3
"""Cron wrapper: the daily seed hunt (event-seed-discovery `hunt`,
engine/seed_hunt) — web search, known organisations' calendars, the sites
events link to, and weekly the Codex-from-memory proposer; new seeds land
`proposed` before the 04:45 seed review trials them.

A hunt spends up to its Firecrawl budget and outlasts Hermes' cron script
timeout, so it is detached; output goes to state/logs/seed-hunt-<date>.log.
Arguments pass through (default: the skill's own budget)."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from engine import db

SKILL = Path("/home/agent/.hermes/skills/business/event-seed-discovery/discover.py")


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    db.migrate()  # a code update must not meet an older schema
    python = os.environ.get("PYTHON", sys.executable)
    log_dir = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"seed-hunt-{date.today().isoformat()}.log"
    with open(log, "ab") as fh:
        proc = subprocess.Popen(
            [python, str(SKILL), "hunt", *sys.argv[1:]],
            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"event-seed-discovery hunt started (pid {proc.pid}); log: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
