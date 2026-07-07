#!/usr/bin/env python3
"""newsletter-weekly — compose a Listmonk campaign from TownLoops events.

Subcommands:
  compose --week 2026-W28 [--from ISO] [--to ISO]
  push    --week 2026-W28 [--send-at ISO]
  show    --week 2026-W28
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/app")
from engine import db, listmonk_api, townloops_api  # noqa: E402

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
STATE_DIR = HERMES_HOME / "state"
NEWSLETTERS_DIR = STATE_DIR / "newsletters"
TEMPLATE_DIR = Path(__file__).parent


def _iso_week_dates(iso_week: str) -> tuple[date, date]:
    """'2026-W28' → (Mon, Sun) of that week."""
    year, wk = iso_week.split("-W")
    monday = date.fromisocalendar(int(year), int(wk), 1)
    return monday, monday + timedelta(days=6)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_active_ads() -> dict[str, list[dict]]:
    """Placeholder — sponsor-management populates ad_spots when SPONSORS_ENABLED.
    Groups active spots by placement."""
    if os.environ.get("SPONSORS_ENABLED", "false").lower() != "true":
        return {}
    conn = db.connect()
    now = _now()
    rows = conn.execute(
        "SELECT * FROM ad_spots WHERE (starts_at IS NULL OR starts_at <= ?) "
        "AND (ends_at IS NULL OR ends_at >= ?) ORDER BY placement, weight DESC",
        (now, now),
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["placement"], []).append(dict(r))
    conn.close()
    return grouped


def cmd_compose(args) -> int:
    monday, sunday = _iso_week_dates(args.week)
    tl = townloops_api.Client()
    events = list(
        tl.paginate(
            "events",
            **{
                "from": f"{monday}T00:00:00",
                "to": f"{sunday}T23:59:59",
                "status": "published",
            },
        )
    )
    if not events:
        sys.stderr.write(f"[newsletter] 0 events for {args.week} — nothing to send.\n")
    events.sort(key=lambda e: e.get("start_at") or "")

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("template.html")
    body = tmpl.render(
        town_slug=tl.town_slug,
        iso_week=args.week,
        week_of=f"{monday:%b %-d} – {sunday:%b %-d, %Y}",
        events=events,
        ads=_load_active_ads(),
    )

    NEWSLETTERS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = NEWSLETTERS_DIR / f"{args.week}.html"
    out_path.write_text(body)

    conn = db.connect()
    conn.execute(
        """
        INSERT INTO newsletters(iso_week, composed_at, event_count, body_path, status)
        VALUES (?, ?, ?, ?, 'draft')
        ON CONFLICT(iso_week) DO UPDATE SET
            composed_at = excluded.composed_at,
            event_count = excluded.event_count,
            body_path   = excluded.body_path,
            status      = 'draft'
        """,
        (args.week, _now(), len(events), str(out_path)),
    )
    conn.commit()
    conn.close()

    print(json.dumps({"week": args.week, "events": len(events), "body": str(out_path)}))
    return 0


def cmd_push(args) -> int:
    conn = db.connect()
    row = conn.execute(
        "SELECT * FROM newsletters WHERE iso_week = ?", (args.week,)
    ).fetchone()
    if not row:
        sys.stderr.write(f"[newsletter] no compose for {args.week}. run compose first.\n")
        return 2
    body = Path(row["body_path"]).read_text()

    monday, _ = _iso_week_dates(args.week)
    send_at = args.send_at or f"{monday}T07:00:00-08:00"

    lm = listmonk_api.Client()
    list_id = int(os.environ["LISTMONK_LIST_ID"])
    campaign = lm.create_campaign(
        name=f"Weekly events {args.week}",
        subject=f"This week in {os.environ['TOWNLOOPS_TOWN_SLUG']}",
        lists=[list_id],
        body=body,
    )
    campaign_id = campaign.get("id") or campaign.get("data", {}).get("id")
    lm.schedule_campaign(campaign_id, send_at)

    conn.execute(
        "UPDATE newsletters SET pushed_at = ?, campaign_id = ?, scheduled_send_at = ?, status = 'scheduled' "
        "WHERE iso_week = ?",
        (_now(), campaign_id, send_at, args.week),
    )
    conn.commit()
    conn.close()

    print(json.dumps({"week": args.week, "campaign_id": campaign_id, "send_at": send_at}))
    return 0


def cmd_show(args) -> int:
    conn = db.connect()
    row = conn.execute("SELECT * FROM newsletters WHERE iso_week = ?", (args.week,)).fetchone()
    print(json.dumps(dict(row) if row else None, default=str, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compose")
    c.add_argument("--week", required=True, help='ISO week like "2026-W28"')
    c.set_defaults(func=cmd_compose)

    ph = sub.add_parser("push")
    ph.add_argument("--week", required=True)
    ph.add_argument("--send-at", dest="send_at", help="ISO datetime with tz; default Mon 07:00 -08:00")
    ph.set_defaults(func=cmd_push)

    sh = sub.add_parser("show")
    sh.add_argument("--week", required=True)
    sh.set_defaults(func=cmd_show)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
