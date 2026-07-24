#!/usr/bin/env python3
"""onboarding-orchestrator — drive the per-town autonomous loop.

Called by cron/jobs.json onboarding-tick (hourly, no_agent: true). Each
invocation runs one bounded orchestrator step; state persists in
agent_state (state/townloops.db).
"""
from __future__ import annotations

import argparse
import json
import sys

from engine import orchestrator


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="onboarding-orchestrator")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick", help="Run one phase step; update state; emit JSON status")
    sub.add_parser("status", help="Print current agent_state as JSON")
    sub.add_parser("finalize",
                   help="Force phase → steady_state (testing / recovery)")
    sm = sub.add_parser("summary",
                        help="Nightly digest: write state/reports/YYYY-MM-DD.md + Telegram")
    sm.add_argument("--hours", type=int, default=24,
                    help="Look-back window in hours (default 24)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "tick":
        st = orchestrator.run_tick()
        return 0 if not st.get("error") else 1
    if args.cmd == "status":
        print(json.dumps(orchestrator.get_status(), ensure_ascii=False))
        return 0
    if args.cmd == "finalize":
        print(json.dumps(orchestrator.finalize(), ensure_ascii=False))
        return 0
    if args.cmd == "summary":
        print(json.dumps(orchestrator.summary(hours=args.hours), ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
