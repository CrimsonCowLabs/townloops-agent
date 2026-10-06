#!/usr/bin/env python3
"""Cron wrapper: daily event-find pass.
Delegates to skills/business/event-find/find.py against every approved seed."""
from __future__ import annotations

import os
import sys
from pathlib import Path

SKILL = Path("/home/agent/.hermes/skills/business/event-find/find.py")


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    # exec under $PYTHON, not in-process: Hermes runs cron scripts with the
    # gateway venv's interpreter, which lacks the skill deps (feedparser etc.)
    # in the docker image. Same convention as engine.orchestrator._PYTHON.
    python = os.environ.get("PYTHON", sys.executable)
    os.execv(python, [python, str(SKILL)])


if __name__ == "__main__":
    sys.exit(main())
