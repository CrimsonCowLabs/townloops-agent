#!/usr/bin/env python3
"""Cron wrapper: daily TownLoops OpenAPI snapshot + breaking-change check."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


API_SKILL = Path("/home/agent/.hermes/skills/business/townloops-api/api.py")


def main() -> int:
    if not API_SKILL.exists():
        print(f"api skill missing: {API_SKILL}", file=sys.stderr)
        return 1
    sys.argv = [str(API_SKILL), "check"]
    runpy.run_path(str(API_SKILL), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
