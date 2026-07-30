#!/usr/bin/env python3
"""Cron wrapper: workqueue drain heartbeat.

Runs every 10 min. For each next_action in the workqueue, claim up to
N items and invoke the matching skill. Skills that don't exist yet
(M2/M3 milestones haven't landed) are quietly skipped — no error spam.
"""
from __future__ import annotations

import json
import sys

from engine import orchestrator


def main() -> int:
    result = orchestrator.drain_workqueue()
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
