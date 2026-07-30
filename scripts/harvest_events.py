#!/usr/bin/env python3
"""Cron wrapper: harvest event sources from entities.

Delegates to skills/business/event-source-harvest/harvest.py — this thin
shim is here purely because Hermes' cron `script:` field must live under
$HERMES_HOME/scripts/, and the actual skill lives elsewhere.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


HARVEST_PATH = Path("/home/agent/.hermes/skills/business/event-source-harvest/harvest.py")


def main() -> int:
    if not HARVEST_PATH.exists():
        print(f"harvest script missing: {HARVEST_PATH}", file=sys.stderr)
        return 1
    # Default cron run: --limit 100 (weekly re-harvest at Sat 05:00).
    sys.argv = [str(HARVEST_PATH), "--limit", "100"]
    runpy.run_path(str(HARVEST_PATH), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
