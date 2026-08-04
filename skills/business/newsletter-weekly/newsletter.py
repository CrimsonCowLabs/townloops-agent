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
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")
from engine import db, listmonk_api, townloops_api  # noqa: E402
from engine.log import get_logger  # noqa: E402

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402


log = get_logger("newsletter")


def _agent_tz() -> ZoneInfo:
    """IANA timezone per $AGENT_TZ (default America/Los_Angeles for legacy SLT parity)."""
    return ZoneInfo(os.environ.get("AGENT_TZ", "America/Los_Angeles"))


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
STATE_DIR = HERMES_HOME / "state"
NEWSLETTERS_DIR = STATE_DIR / "newsletters"
TEMPLATE_DIR = Path(__file__).parent


def _iso_week_dates(iso_week: str) -> tuple[date, date]:
    """'2026-W28' → (Mon, Sun) of that week."""
    year, wk = iso_week.split("-W")
    monday = date.fromisocalendar(int(year), int(wk), 1)
    return monday, monday + timedelta(days=6)


def _current_iso_week() -> str:
    """Today's ISO week as '2026-W28'."""
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


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
        log.warning("no events for week", extra={"week": args.week})
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
        log.error("no compose for week", extra={"week": args.week,
                                                  "hint": "run compose first"})
        return 2
    body = Path(row["body_path"]).read_text()

    monday, _ = _iso_week_dates(args.week)
    # Monday 07:00 in the agent's local timezone. zoneinfo produces the right
    # offset for the date (handles DST) — no more hardcoded -08:00 that sends
    # newsletters 3 hours early during PDT or on east-coast deployments.
    send_at = args.send_at or datetime(
        monday.year, monday.month, monday.day, 7, 0, 0, tzinfo=_agent_tz()
    ).isoformat()

    # Safety gate — this creates a real Listmonk campaign scheduled to hit
    # every subscriber. Default is preview-only; require an explicit opt-in
    # to actually schedule. A stray `newsletter push --week ...` cron edit
    # or a typo in --week should never send a real campaign.
    if not args.yes_really_send:
        print(json.dumps({
            "dry_run": True,
            "week": args.week,
            "planned_send_at": send_at,
            "subject": f"This week in {os.environ['TOWNLOOPS_TOWN_SLUG']}",
            "listmonk_list_id": int(os.environ["LISTMONK_LIST_ID"]),
            "body_bytes": len(body),
            "note": "Pass --yes-really-send to actually create + schedule the campaign.",
        }, indent=2))
        return 0

    lm = listmonk_api.Client()
    list_id = int(os.environ["LISTMONK_LIST_ID"])
    campaign = lm.create_campaign(
        name=f"Weekly events {args.week}",
        subject=f"This week in {os.environ['TOWNLOOPS_TOWN_SLUG']}",
        lists=[list_id],
        body=body,
    )
    # listmonk_api.Client._req already unwraps the outer {data: ...} envelope,
    # so the response IS the campaign row. Missing `id` is a hard error — do
    # not fall through to `schedule_campaign(None, ...)` silently.
    campaign_id = campaign.get("id") if isinstance(campaign, dict) else None
    if not campaign_id:
        log.error("Listmonk create_campaign returned no id",
                    extra={"response_sample": json.dumps(campaign, default=str)[:400]})
        return 3
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
    week = args.week or _current_iso_week()
    conn = db.connect()
    row = conn.execute("SELECT * FROM newsletters WHERE iso_week = ?", (week,)).fetchone()
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
    ph.add_argument("--send-at", dest="send_at",
                     help="ISO datetime with tz; default Mon 07:00 in $AGENT_TZ (America/Los_Angeles if unset)")
    ph.add_argument("--yes-really-send", dest="yes_really_send", action="store_true",
                     help="Actually create + schedule the Listmonk campaign. Without this flag, `push` prints a preview and exits.")
    ph.set_defaults(func=cmd_push)

    sh = sub.add_parser("show")
    sh.add_argument("--week", default=None,
                    help='ISO week like "2026-W28"; defaults to the current week')
    sh.set_defaults(func=cmd_show)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
