#!/usr/bin/env python3
"""Cron wrapper: run one orchestrator tick.

Invoked by the `onboarding-tick` cron job. Hermes' no_agent cron path
expects a `script:` field pointing at a file under $HERMES_HOME/scripts/,
so per-town cron entries reference this file rather than a shell command.
"""
from __future__ import annotations

import json
import sys

from engine import orchestrator


def main() -> int:
    st = orchestrator.run_tick()
    print(json.dumps(st, ensure_ascii=False))
    return 0 if not st.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
