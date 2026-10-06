#!/usr/bin/env python3
"""Cron wrapper: daily TownLoops OpenAPI snapshot + breaking-change check."""
from __future__ import annotations

import os
import sys
from pathlib import Path


API_SKILL = Path("/home/agent/.hermes/skills/business/townloops-api/api.py")


def main() -> int:
    if not API_SKILL.exists():
        print(f"api skill missing: {API_SKILL}", file=sys.stderr)
        return 1
    # exec under $PYTHON, not in-process: Hermes runs cron scripts with the
    # gateway venv's interpreter, which lacks the skill deps (feedparser etc.)
    # in the docker image. Same convention as engine.orchestrator._PYTHON.
    python = os.environ.get("PYTHON", sys.executable)
    os.execv(python, [python, str(API_SKILL), "check"])


if __name__ == "__main__":
    sys.exit(main())
