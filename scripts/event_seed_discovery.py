#!/usr/bin/env python3
"""Cron wrapper: run event-seed-discovery weekly + on first-run.
Delegates to skills/business/event-seed-discovery/discover.py."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from engine import db

SKILL = Path("/home/agent/.hermes/skills/business/event-seed-discovery/discover.py")


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    db.migrate()  # a code update must not meet an older schema
    # exec under $PYTHON, not in-process: Hermes runs cron scripts with the
    # gateway venv's interpreter, which lacks the skill deps (feedparser etc.)
    # in the docker image. Same convention as engine.orchestrator._PYTHON.
    python = os.environ.get("PYTHON", sys.executable)
    os.execv(python, [python, str(SKILL), "discover", "--limit", "15"])


if __name__ == "__main__":
    sys.exit(main())
