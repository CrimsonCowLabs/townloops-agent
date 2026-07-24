#!/usr/bin/env python3
"""performer-enrichment — thin wrapper. Shared loop lives in engine/enrich_cli.py."""
import sys
sys.path.insert(0, "/app")
from engine.enrich_cli import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run("performer"))
