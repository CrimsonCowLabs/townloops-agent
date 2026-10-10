#!/usr/bin/env python3
"""series-candidates — weekly, read-only: what a curator could do with
TownLoops Series (ADR 0007). Logic: `engine.series.candidates`.

Lists Clusters (published live Events whose titles share a `series_key`)
with no Series that pass TownLoops' own rule — two or more distinct
town-local dates at a single resolved Venue, performer-name Clusters left
out — and existing Series that now span several venues or have two
occurrences on one date (time to run the Series audit). Writes
state/reports/series-candidates-<date>.md and, when there is anything to
say, a Telegram summary. Never acts on it: creating a Series is a curator
action with no API write surface.

Usage: report.py [--days-back N] [--days-ahead N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dateutil import tz as _tz

from engine import series
from engine.derive_helpers import open_db, record_activity, utc_z
from engine.event_extract import town_tz_name


_WORKER = "series-candidates"


def _performer_names(conn) -> list[str]:
    return [r[0] for t in ("performers", "creators")
            for r in conn.execute(f"SELECT name FROM {t} WHERE COALESCE(name, '') != ''")]


def _markdown(town: str, report: series.CandidatesReport, window: str) -> str:
    lines = [f"# {town} — Series candidates {date.today().isoformat()}", "",
             f"Published live events {window}. Read-only: creating, editing or archiving a "
             "Series is a curator action.", "",
             "## Clusters that could become a Series", ""]
    if not report.clusters:
        lines.append("No Clusters pass the rule (two or more dates at one venue, not a "
                     "performer's name).")
    for c in report.clusters:
        lines.append(f"- **{c.title}** (`{c.key}`) — {c.dates} dates at "
                     f"`{c.venue_slug or c.venue_id}`: {', '.join(c.slugs)}")
    lines += ["", "## Series that span several venues", ""]
    lines += [f"- `{d.slug}` — {d.venues} venues" for d in report.multi_venue] or ["None."]
    lines += ["", "## Series with two or more occurrences on one date", ""]
    lines += [f"- `{d.slug}` — {', '.join(d.dates)}" for d in report.duplicate_dates] or ["None."]
    return "\n".join(lines) + "\n"


def run(client, conn, days_back: int, days_ahead: int,
        now: Optional[datetime] = None) -> tuple[Path, series.CandidatesReport]:
    now = now or datetime.now(timezone.utc)
    client.verify_town()
    series.refresh_snapshot(client)
    events = list(client.paginate("events", status="published",
                                  **{"from": utc_z(now - timedelta(days=days_back)),
                                     "to": utc_z(now + timedelta(days=days_ahead))}))
    report = series.candidates(events, _tz.gettz(town_tz_name()), _performer_names(conn))
    town = os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")
    out_dir = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"series-candidates-{now.date().isoformat()}.md"
    path.write_text(_markdown(town, report, f"from {days_back} days back to {days_ahead} ahead"))
    # For the nightly digest's Series section (engine/orchestrator.summary).
    (out_dir / "series-candidates-latest.json").write_text(json.dumps({
        "generated_at": now.isoformat(timespec="seconds"),
        "clusters": [asdict(c) for c in report.clusters],
        "multi_venue": [asdict(d) for d in report.multi_venue],
        "duplicate_dates": [asdict(d) for d in report.duplicate_dates]}, indent=1))
    return path, report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="series-candidates")
    ap.add_argument("--days-back", type=int, default=90)
    ap.add_argument("--days-ahead", type=int, default=180)
    args = ap.parse_args(argv)
    from engine.townloops_api import Client
    conn = open_db()
    path, report = run(Client(), conn, args.days_back, args.days_ahead)
    counts = {"clusters": len(report.clusters), "multi_venue": len(report.multi_venue),
              "duplicate_dates": len(report.duplicate_dates)}
    if any(counts.values()):
        from engine.orchestrator import _telegram_notify
        _telegram_notify(
            f"[{os.environ.get('TOWNLOOPS_TOWN_SLUG', '?')}] Series candidates: "
            f"{counts['clusters']} Cluster(s) could become a Series; "
            f"{counts['multi_venue']} Series span several venues; "
            f"{counts['duplicate_dates']} have same-day duplicates. Report: {path.name}")
    record_activity(conn, _WORKER, "report", path.name, detail=json.dumps(counts))
    print(json.dumps({"report": str(path), **counts}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
