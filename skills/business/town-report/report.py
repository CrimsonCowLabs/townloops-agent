#!/usr/bin/env python3
"""town-report — the daily town report and the analyst run's budget (#64).
Logic: engine/town_report.py, engine/alerts.py, engine/analyst_runs.py.

Subcommands:
  build [--hours N | --since ISO] [--narrative FILE] [--send]
        tick the immediate alerts, then write state/reports/<date>-analyst.md
        (Needs you, standing alerts, sources, events, entities, holds,
        failures, analyst runs); --send also Telegrams the short version
  alerts
        the standing alerts as JSON (what "Needs you" is built from)
  budget start
        back up the state DBs, mark runs left running past their time
        interrupted, open a run from town-profile.yaml `analyst:`; prints
        its run_id (exit 1 while another run is still within its time)
  budget status ID
        budget, used (minutes, Firecrawl requests, submissions) and remaining
  budget finish ID --status ok|failed [--report PATH] [--notes TEXT]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from engine import alerts, analyst_runs, db, local_data, town_report
from engine.derive_helpers import open_db


def _build(args) -> int:
    narrative = Path(args.narrative).read_text() if args.narrative else None
    out = town_report.run(open_db(), since=args.since, hours=args.hours,
                          narrative=narrative, send=args.send)
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


def _alerts(args) -> int:
    print(json.dumps(alerts.as_dicts(alerts.standing(open_db())), indent=2, ensure_ascii=False))
    return 0


def _budget(args) -> int:
    conn = open_db()
    try:
        if args.verb == "start":
            out = analyst_runs.start(conn, backup=local_data.backup)
        elif args.verb == "status":
            out = analyst_runs.status(conn, args.id)
        else:
            out = analyst_runs.finish(conn, args.id, args.status, report=args.report,
                                      notes=args.notes)
    except (analyst_runs.RunActive, KeyError, ValueError) as e:
        print(json.dumps({"error": str(e).strip("'\"")}))
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="town-report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--hours", type=int, default=24)
    b.add_argument("--since", help="ISO UTC instant (overrides --hours)")
    b.add_argument("--narrative", help="a file with the analyst run's own write-up")
    b.add_argument("--send", action="store_true", help="also send the Telegram summary")
    sub.add_parser("alerts")
    bud = sub.add_parser("budget")
    verbs = bud.add_subparsers(dest="verb", required=True)
    verbs.add_parser("start")
    st = verbs.add_parser("status")
    st.add_argument("id", type=int)
    fin = verbs.add_parser("finish")
    fin.add_argument("id", type=int)
    fin.add_argument("--status", required=True, choices=("ok", "failed"))
    fin.add_argument("--report")
    fin.add_argument("--notes")
    args = ap.parse_args(argv)
    db.migrate()   # analyst_runs / job_health may be newer than the town DB
    return {"build": _build, "alerts": _alerts, "budget": _budget}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
