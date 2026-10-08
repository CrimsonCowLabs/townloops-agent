#!/usr/bin/env python3
"""Cron wrapper: harvest event sources from entities.

Delegates to skills/business/event-source-harvest/harvest.py — this thin
shim is here purely because Hermes' cron `script:` field must live under
$HERMES_HOME/scripts/, and the actual skill lives elsewhere.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from engine import db


HARVEST_PATH = Path("/home/agent/.hermes/skills/business/event-source-harvest/harvest.py")


def main() -> int:
    if not HARVEST_PATH.exists():
        print(f"harvest script missing: {HARVEST_PATH}", file=sys.stderr)
        return 1
    db.migrate()  # a code update must not meet an older schema
    # Default cron run: --limit 100 (weekly re-harvest at Sat 05:00).
    # exec under $PYTHON, not in-process: Hermes runs cron scripts with the
    # gateway venv's interpreter, which lacks the skill deps (feedparser etc.)
    # in the docker image. Same convention as engine.orchestrator._PYTHON.
    python = os.environ.get("PYTHON", sys.executable)
    os.execv(python, [python, str(HARVEST_PATH), "--limit", "100"])


if __name__ == "__main__":
    sys.exit(main())
