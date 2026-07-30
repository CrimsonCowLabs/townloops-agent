#!/usr/bin/env python3
"""Cron wrapper: nightly successes/failures digest."""
from __future__ import annotations

import json
import sys

from engine import orchestrator


def main() -> int:
    result = orchestrator.summary(hours=24)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
