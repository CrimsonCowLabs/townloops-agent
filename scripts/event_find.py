#!/usr/bin/env python3
"""Cron wrapper: daily event-find pass.
Delegates to skills/business/event-find/find.py against every approved seed."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

SKILL = Path("/home/agent/.hermes/skills/business/event-find/find.py")


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    sys.argv = [str(SKILL)]
    runpy.run_path(str(SKILL), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
