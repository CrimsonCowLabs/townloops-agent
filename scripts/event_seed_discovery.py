#!/usr/bin/env python3
"""Cron wrapper: run event-seed-discovery weekly + on first-run.
Delegates to skills/business/event-seed-discovery/discover.py. With no
arguments (a Hermes profile's no_agent cron) it runs the weekly discover;
given arguments (e.g. docker's `probe --limit 4`) it passes them through."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from engine import db

SKILL = Path("/home/agent/.hermes/skills/business/event-seed-discovery/discover.py")
DEFAULT_ARGS = ["discover", "--limit", "15"]


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    db.migrate()  # a code update must not meet an older schema
    # exec under $PYTHON, not in-process: Hermes runs cron scripts with the
    # gateway venv's interpreter, which lacks the skill deps (feedparser etc.)
    # in the docker image. Same convention as engine.orchestrator._PYTHON.
    python = os.environ.get("PYTHON", sys.executable)
    os.execv(python, [python, str(SKILL), *(sys.argv[1:] or DEFAULT_ARGS)])


if __name__ == "__main__":
    sys.exit(main())
