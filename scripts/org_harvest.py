#!/usr/bin/env python3
"""Cron wrapper: weekly organisation harvest (autonomous mode) — sweep and
verify businesses, community groups and non-profits from town-profile.yaml →
`discovery`, and approve the verified ones (engine/autonomy.judge_candidate);
the business-promote job then submits them as pending. In supervised mode
the skill reports itself inactive (sweeps are run by hand there).

Sweeps outlast Hermes' cron script timeout, so the run is detached; output
goes to state/logs/org-harvest-<date>.log."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from engine import db

SKILL = Path("/home/agent/.hermes/skills/business/business-discovery/discover.py")


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    db.migrate()  # a code update must not meet an older schema
    python = os.environ.get("PYTHON", sys.executable)
    log_dir = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"org-harvest-{date.today().isoformat()}.log"
    with open(log, "ab") as fh:
        proc = subprocess.Popen(
            [python, str(SKILL), "harvest", *sys.argv[1:]],
            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"business-discovery harvest started (pid {proc.pid}); log: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
