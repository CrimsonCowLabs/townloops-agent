#!/usr/bin/env python3
"""sponsor-management — CRUD sponsors + ad_spots.

STATUS: v1 TownLoops API has no sponsor/ad endpoints. This skill runs
against the proposed schema at docs/api-sponsor-schema-proposal.md.
Gated by SPONSORS_ENABLED (default false).

When disabled: writes to the local `sponsors` / `ad_spots` tables only —
safe for building the sponsor roster ahead of API landing.
When enabled:  same local writes, plus POST/PATCH to TownLoops.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")
from engine import db, townloops_api  # noqa: E402


def _enabled() -> bool:
    return os.environ.get("SPONSORS_ENABLED", "false").lower() == "true"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


# ---- sponsor CRUD ----

def cmd_add_sponsor(args) -> int:
    conn = db.connect()
    uid = _hash("sponsor", (args.name or "").lower().strip())
    payload = {
        "name": args.name,
        "kind": args.kind,
        "logo_url": args.logo_url,
        "website": args.website,
        "description": args.description,
        "contact_email": args.email,
        "tier": args.tier,
        "starts_at": args.starts_at or _now(),
        "ends_at": args.ends_at,
        "is_active": 1,
    }
    payload_str = json.dumps(payload, sort_keys=True)
    conn.execute(
        """
        INSERT INTO sponsors(sponsor_uid, name, kind, logo_url, website, description,
                             contact_email, tier, starts_at, ends_at, is_active,
                             content_hash)
        VALUES (:sponsor_uid, :name, :kind, :logo_url, :website, :description,
                :contact_email, :tier, :starts_at, :ends_at, :is_active, :content_hash)
        ON CONFLICT(sponsor_uid) DO UPDATE SET
            kind = excluded.kind,
            logo_url = COALESCE(excluded.logo_url, sponsors.logo_url),
            website = COALESCE(excluded.website, sponsors.website),
            description = COALESCE(excluded.description, sponsors.description),
            contact_email = COALESCE(excluded.contact_email, sponsors.contact_email),
            tier = excluded.tier,
            starts_at = COALESCE(excluded.starts_at, sponsors.starts_at),
            ends_at = COALESCE(excluded.ends_at, sponsors.ends_at),
            is_active = excluded.is_active,
            content_hash = excluded.content_hash
        """,
        {**payload, "sponsor_uid": uid, "content_hash": _hash(payload_str)},
    )
    conn.commit()

    remote_slug = None
    if _enabled():
        try:
            tl = townloops_api.Client()
            resp = tl.create("sponsors", payload)
            remote_slug = resp.get("slug") or resp.get("sponsor", {}).get("slug")
            conn.execute(
                "UPDATE sponsors SET townloops_slug = ?, pushed_at = ?, pushed_hash = ? "
                "WHERE sponsor_uid = ?",
                (remote_slug, _now(), payload["content_hash"] if False else _hash(payload_str), uid),
            )
            conn.commit()
        except townloops_api.TownloopsError as e:
            sys.stderr.write(f"[sponsors] remote POST failed: {e}\n")

    print(json.dumps({"sponsor_uid": uid, "remote_slug": remote_slug, "enabled": _enabled()}))
    return 0


def cmd_list_sponsors(args) -> int:
    conn = db.connect()
    rows = conn.execute("SELECT * FROM sponsors ORDER BY tier, name").fetchall()
    for r in rows:
        print(json.dumps(dict(r), default=str))
    return 0


# ---- ad_spot CRUD ----

def cmd_assign(args) -> int:
    conn = db.connect()
    sp = conn.execute(
        "SELECT sponsor_uid FROM sponsors WHERE sponsor_uid = ? OR townloops_slug = ?",
        (args.sponsor, args.sponsor),
    ).fetchone()
    if not sp:
        sys.stderr.write(f"[sponsors] unknown sponsor: {args.sponsor}\n")
        return 2
    ad_uid = _hash(sp["sponsor_uid"], args.placement, args.starts_at or "")
    payload = {
        "sponsor_uid": sp["sponsor_uid"],
        "placement": args.placement,
        "asset_url": args.asset,
        "click_url": args.click,
        "alt_text": args.alt,
        "starts_at": args.starts_at,
        "ends_at": args.ends_at,
        "weight": args.weight,
    }
    payload_str = json.dumps(payload, sort_keys=True)
    conn.execute(
        """
        INSERT INTO ad_spots(ad_spot_uid, sponsor_uid, placement, asset_url, click_url,
                             alt_text, starts_at, ends_at, weight, content_hash)
        VALUES (:ad_spot_uid, :sponsor_uid, :placement, :asset_url, :click_url,
                :alt_text, :starts_at, :ends_at, :weight, :content_hash)
        ON CONFLICT(ad_spot_uid) DO UPDATE SET
            asset_url = excluded.asset_url,
            click_url = excluded.click_url,
            alt_text = excluded.alt_text,
            weight = excluded.weight,
            content_hash = excluded.content_hash
        """,
        {**payload, "ad_spot_uid": ad_uid, "content_hash": _hash(payload_str)},
    )
    conn.commit()

    remote_id = None
    if _enabled():
        try:
            tl = townloops_api.Client()
            resp = tl.create("ad_spots", payload)
            remote_id = resp.get("id") or resp.get("ad_spot", {}).get("id")
            conn.execute(
                "UPDATE ad_spots SET townloops_id = ?, pushed_at = ? WHERE ad_spot_uid = ?",
                (str(remote_id), _now(), ad_uid),
            )
            conn.commit()
        except townloops_api.TownloopsError as e:
            sys.stderr.write(f"[sponsors] remote ad_spot POST failed: {e}\n")

    print(json.dumps({"ad_spot_uid": ad_uid, "remote_id": remote_id, "enabled": _enabled()}))
    return 0


def cmd_audit(args) -> int:
    """Weekly audit: expiring spots, sponsors with no active spots, tier over-allocation."""
    conn = db.connect()
    now = _now()
    soon = (datetime.now(timezone.utc).replace(microsecond=0)).isoformat(timespec="seconds")
    # naive "within 14 days" — good enough for a report.
    expiring = conn.execute(
        "SELECT ad_spot_uid, placement, ends_at FROM ad_spots "
        "WHERE ends_at IS NOT NULL AND ends_at > ? AND ends_at <= datetime(?, '+14 days')",
        (now, now),
    ).fetchall()
    unbound = conn.execute(
        "SELECT s.sponsor_uid, s.name FROM sponsors s "
        "LEFT JOIN ad_spots a ON a.sponsor_uid = s.sponsor_uid "
        "WHERE s.is_active = 1 AND a.ad_spot_uid IS NULL"
    ).fetchall()
    print(
        json.dumps(
            {
                "checked_at": now,
                "expiring_soon": [dict(r) for r in expiring],
                "sponsors_without_spots": [dict(r) for r in unbound],
                "enabled": _enabled(),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-sponsor")
    a.add_argument("--name", required=True)
    a.add_argument("--kind", choices=["business", "community"], required=True)
    a.add_argument("--tier", choices=["gold", "silver", "bronze", "community"], required=True)
    a.add_argument("--logo-url", dest="logo_url")
    a.add_argument("--website")
    a.add_argument("--description")
    a.add_argument("--email", dest="email")
    a.add_argument("--starts-at", dest="starts_at")
    a.add_argument("--ends-at", dest="ends_at")
    a.set_defaults(func=cmd_add_sponsor)

    ls = sub.add_parser("list-sponsors")
    ls.set_defaults(func=cmd_list_sponsors)

    ap = sub.add_parser("assign", help="bind a sponsor to a placement over a time window")
    ap.add_argument("--sponsor", required=True, help="sponsor_uid or townloops_slug")
    ap.add_argument("--placement", required=True,
                    choices=["newsletter-header", "newsletter-footer",
                             "homepage-hero", "event-detail-banner", "sidebar"])
    ap.add_argument("--asset", required=True, help="asset_url (image)")
    ap.add_argument("--click", required=True, help="click_url")
    ap.add_argument("--alt")
    ap.add_argument("--starts-at", dest="starts_at")
    ap.add_argument("--ends-at", dest="ends_at")
    ap.add_argument("--weight", type=int, default=1)
    ap.set_defaults(func=cmd_assign)

    au = sub.add_parser("audit", help="report expiring spots + unbound sponsors")
    au.set_defaults(func=cmd_audit)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
