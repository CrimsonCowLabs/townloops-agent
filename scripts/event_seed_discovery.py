#!/usr/bin/env python3
"""Cron wrapper: run event-seed-discovery weekly + on first-run.
Delegates to skills/business/event-seed-discovery/discover.py."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

SKILL = Path("/home/agent/.hermes/skills/business/event-seed-discovery/discover.py")


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    sys.argv = [str(SKILL), "discover", "--limit", "15"]
    runpy.run_path(str(SKILL), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
