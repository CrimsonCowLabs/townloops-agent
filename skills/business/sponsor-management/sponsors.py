#!/usr/bin/env python3
"""sponsor-management — CRUD sponsors + ad-slots against the TownLoops API.

The TownLoops v1 API landed sponsor + ad-slot endpoints (see openapi.json
under /towns/{slug}/sponsors and /towns/{slug}/ad-slots). This skill talks
to those endpoints directly. SPONSORS_ENABLED still gates the remote push
so operators can build a local sponsor roster without shipping.

Payload shape (canonical):

  SponsorPayload  = {name, image_alt, [slug, blurb, image_url|image_path, link_url, is_active]}
  AdSlotPayload   = {placement, sponsor_slug, starts_at, [ends_at, priority, is_active]}

Local DB (sponsors, ad_spots) still uses the older column names for
history — the translation happens here at POST time so we don't churn
every row on a rename.
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


# Placements the ad-slot API accepts (matches openapi.json AdSlotPayload.placement enum).
AD_SLOT_PLACEMENTS = ("homepage", "newsletter", "category", "featured_event", "sidebar")


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


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# =====================================================================
# Remote payload builders
# =====================================================================


def _sponsor_remote_payload(row: dict) -> dict:
    """Translate a local `sponsors` row into a SponsorPayload the API accepts.

    Drops kind/tier/contact_email/starts_at/ends_at (server doesn't take them).
    Renames logo_url→image_url, description→blurb, website→link_url.
    image_alt must be present (server requires it).
    """
    return _drop_none({
        "name": row["name"],
        "slug": row.get("townloops_slug"),  # let server auto if None
        "blurb": row.get("description"),
        "image_url": row.get("logo_url"),
        "image_alt": row.get("image_alt"),
        "link_url": row.get("website"),
        "is_active": bool(row.get("is_active", 1)),
    })


def _ad_slot_remote_payload(row: dict, sponsor_slug: str) -> dict:
    """Translate a local `ad_spots` row into an AdSlotPayload.

    starts_at is REQUIRED by the server; caller must have populated it.
    Old asset_url/click_url/alt_text now live on the sponsor, not the slot,
    so they're dropped from the remote payload.
    """
    return _drop_none({
        "placement": row["placement"],
        "sponsor_slug": sponsor_slug,
        "starts_at": row["starts_at"],
        "ends_at": row.get("ends_at"),
        "priority": row.get("weight"),  # local column 'weight' → API 'priority'
        "is_active": True,
    })


# =====================================================================
# sponsor CRUD
# =====================================================================


def cmd_add_sponsor(args) -> int:
    conn = db.connect()
    uid = _hash("sponsor", (args.name or "").lower().strip())

    # Local row keeps the classification columns (kind/tier/contact_email/starts_at/
    # ends_at) so audit/reporting stays useful, even though the API doesn't take them.
    local_row = {
        "sponsor_uid": uid,
        "name": args.name,
        "kind": args.kind,
        "logo_url": args.image_url,
        "website": args.link_url,
        "description": args.blurb,
        "contact_email": args.email,
        "tier": args.tier,
        "starts_at": args.starts_at,
        "ends_at": args.ends_at,
        "image_alt": args.image_alt,
        "is_active": 1,
    }
    payload_str = json.dumps(local_row, sort_keys=True, default=str)
    local_row["content_hash"] = _hash(payload_str)

    conn.execute(
        """
        INSERT INTO sponsors(sponsor_uid, name, kind, logo_url, website, description,
                             contact_email, tier, starts_at, ends_at, image_alt,
                             is_active, content_hash)
        VALUES (:sponsor_uid, :name, :kind, :logo_url, :website, :description,
                :contact_email, :tier, :starts_at, :ends_at, :image_alt,
                :is_active, :content_hash)
        ON CONFLICT(sponsor_uid) DO UPDATE SET
            kind = excluded.kind,
            logo_url = COALESCE(excluded.logo_url, sponsors.logo_url),
            website = COALESCE(excluded.website, sponsors.website),
            description = COALESCE(excluded.description, sponsors.description),
            contact_email = COALESCE(excluded.contact_email, sponsors.contact_email),
            tier = excluded.tier,
            starts_at = COALESCE(excluded.starts_at, sponsors.starts_at),
            ends_at = COALESCE(excluded.ends_at, sponsors.ends_at),
            image_alt = COALESCE(excluded.image_alt, sponsors.image_alt),
            is_active = excluded.is_active,
            content_hash = excluded.content_hash
        """,
        local_row,
    )
    conn.commit()

    remote_slug = None
    if _enabled():
        # image_alt is a hard server requirement; refuse to push without it.
        if not local_row["image_alt"]:
            sys.stderr.write(
                "[sponsors] refusing to POST: image_alt is required by the API "
                "(accessibility mandate). Re-run add-sponsor with --image-alt.\n"
            )
        else:
            remote_payload = _sponsor_remote_payload(local_row)
            try:
                tl = townloops_api.Client()
                resp = tl.create(
                    "sponsors",
                    remote_payload,
                    idempotency_key=f"sponsor:{uid}",
                )
                remote_slug = resp.get("slug") if isinstance(resp, dict) else None
                conn.execute(
                    "UPDATE sponsors SET townloops_slug = ?, pushed_at = ?, pushed_hash = ? "
                    "WHERE sponsor_uid = ?",
                    (remote_slug, _now(), local_row["content_hash"], uid),
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


# =====================================================================
# ad-slot CRUD
# =====================================================================


def cmd_assign(args) -> int:
    conn = db.connect()
    sp = conn.execute(
        "SELECT sponsor_uid, townloops_slug FROM sponsors "
        "WHERE sponsor_uid = ? OR townloops_slug = ?",
        (args.sponsor, args.sponsor),
    ).fetchone()
    if not sp:
        sys.stderr.write(f"[sponsors] unknown sponsor: {args.sponsor}\n")
        return 2

    ad_uid = _hash(sp["sponsor_uid"], args.placement, args.starts_at)
    local_row = {
        "ad_spot_uid": ad_uid,
        "sponsor_uid": sp["sponsor_uid"],
        "placement": args.placement,
        # Kept in the local table for historical continuity; the API no longer
        # accepts them here — those fields moved onto the Sponsor record itself.
        "asset_url": args.asset or "",
        "click_url": args.click or "",
        "alt_text": args.alt,
        "starts_at": args.starts_at,
        "ends_at": args.ends_at,
        "weight": args.priority,
    }
    payload_str = json.dumps(local_row, sort_keys=True, default=str)
    local_row["content_hash"] = _hash(payload_str)

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
            ends_at = excluded.ends_at,
            content_hash = excluded.content_hash
        """,
        local_row,
    )
    conn.commit()

    remote_id = None
    if _enabled():
        if not sp["townloops_slug"]:
            sys.stderr.write(
                "[sponsors] refusing to POST ad-slot: sponsor has no townloops_slug. "
                "Push the sponsor first (rerun add-sponsor with SPONSORS_ENABLED=true).\n"
            )
        else:
            try:
                tl = townloops_api.Client()
                resp = tl.create(
                    "ad-slots",
                    _ad_slot_remote_payload(local_row, sp["townloops_slug"]),
                    idempotency_key=f"ad-slot:{ad_uid}",
                )
                remote_id = resp.get("id") if isinstance(resp, dict) else None
                conn.execute(
                    "UPDATE ad_spots SET townloops_id = ?, pushed_at = ?, pushed_hash = ? "
                    "WHERE ad_spot_uid = ?",
                    (str(remote_id) if remote_id is not None else None,
                     _now(), local_row["content_hash"], ad_uid),
                )
                conn.commit()
            except townloops_api.TownloopsError as e:
                sys.stderr.write(f"[sponsors] remote ad-slot POST failed: {e}\n")

    print(json.dumps({"ad_spot_uid": ad_uid, "remote_id": remote_id, "enabled": _enabled()}))
    return 0


def cmd_audit(args) -> int:
    """Weekly audit: expiring slots, sponsors with no active slots, tier over-allocation."""
    conn = db.connect()
    now = _now()
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
    missing_alt = conn.execute(
        "SELECT sponsor_uid, name FROM sponsors "
        "WHERE is_active = 1 AND (image_alt IS NULL OR image_alt = '')"
    ).fetchall()
    print(
        json.dumps(
            {
                "checked_at": now,
                "expiring_soon": [dict(r) for r in expiring],
                "sponsors_without_spots": [dict(r) for r in unbound],
                "sponsors_missing_image_alt": [dict(r) for r in missing_alt],
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
    a.add_argument("--kind", choices=["business", "community"], required=True,
                   help="Local classification. Not sent to the API.")
    a.add_argument("--tier", choices=["gold", "silver", "bronze", "community"], required=True,
                   help="Local classification. Not sent to the API.")
    a.add_argument("--image-url", dest="image_url",
                   help="Absolute https URL of the sponsor's ad image. Server fetches "
                        "and stores it via the SSRF-guarded pipeline.")
    a.add_argument("--image-alt", dest="image_alt", required=True,
                   help="Alt text for the image. REQUIRED by the API (accessibility mandate).")
    a.add_argument("--link-url", dest="link_url",
                   help="Click-through URL — where the ad sends users.")
    a.add_argument("--blurb", dest="blurb",
                   help="Short blurb shown alongside the image (max 5000 chars).")
    a.add_argument("--email", dest="email",
                   help="Contact email for the sponsor. Local only, not sent to the API.")
    a.add_argument("--starts-at", dest="starts_at",
                   help="Local sponsor-level start (not sent to API — ad-slots carry timing).")
    a.add_argument("--ends-at", dest="ends_at",
                   help="Local sponsor-level end (not sent to API — ad-slots carry timing).")
    a.set_defaults(func=cmd_add_sponsor)

    ls = sub.add_parser("list-sponsors")
    ls.set_defaults(func=cmd_list_sponsors)

    ap = sub.add_parser("assign", help="bind a sponsor to a placement over a time window")
    ap.add_argument("--sponsor", required=True, help="sponsor_uid or townloops_slug")
    ap.add_argument("--placement", required=True, choices=list(AD_SLOT_PLACEMENTS))
    ap.add_argument("--starts-at", dest="starts_at", required=True,
                    help="RFC3339 timestamp. REQUIRED — server needs it for de-dup natural key.")
    ap.add_argument("--ends-at", dest="ends_at",
                    help="RFC3339 timestamp. Optional (open-ended slot if omitted).")
    ap.add_argument("--priority", type=int, default=0,
                    help="0..1000, higher wins in a placement. Defaults to 0.")
    # Legacy asset/click/alt: sponsor now owns them. Kept as no-op locals for
    # backwards CLI compat — they get archived in the local ad_spots row but
    # aren't sent to the server.
    ap.add_argument("--asset", help="[legacy] archived locally; server takes this from the sponsor's image_url now.")
    ap.add_argument("--click", help="[legacy] archived locally; server takes this from the sponsor's link_url now.")
    ap.add_argument("--alt", help="[legacy] archived locally; server takes this from the sponsor's image_alt now.")
    ap.set_defaults(func=cmd_assign)

    au = sub.add_parser("audit", help="report expiring slots + unbound + missing alt-text sponsors")
    au.set_defaults(func=cmd_audit)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
