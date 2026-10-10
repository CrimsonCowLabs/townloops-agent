#!/usr/bin/env python3
"""Cron wrapper: hourly moderation-feedback poll (autonomous mode). With no
pending submissions the skill exits at once without touching TownLoops."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from engine import db

SKILL = Path("/home/agent/.hermes/skills/business/moderation-feedback/poll.py")


def main() -> int:
    if not SKILL.exists():
        print(f"skill missing: {SKILL}", file=sys.stderr)
        return 1
    db.migrate()  # a code update must not meet an older schema
    # $PYTHON, not this interpreter: Hermes runs cron scripts with the gateway
    # venv, which lacks the skill deps. Same convention as event_seed_discovery.py.
    python = os.environ.get("PYTHON", sys.executable)
    os.execv(python, [python, str(SKILL), *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
