#!/usr/bin/env python3
"""Cron wrapper: the event-seed-discovery daily seed review (autonomous mode; inactive in supervised).
A Hermes no_agent `script:` job can't pass arguments, so this pins them;
see event_seed_discovery.py."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from engine import db

SKILL = Path("/home/agent/.hermes/skills/business/event-seed-discovery/discover.py")
DEFAULT_ARGS = ["review"]


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    db.migrate()  # a code update must not meet an older schema
    python = os.environ.get("PYTHON", sys.executable)
    os.execv(python, [python, str(SKILL), *(sys.argv[1:] or DEFAULT_ARGS)])


if __name__ == "__main__":
    sys.exit(main())
