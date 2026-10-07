#!/usr/bin/env python3
"""event-ship — POST one event + its derived entities to TownLoops.

Per plan adversarial pass #19: entities go first (venue, host, people),
then the event. TownLoops references venue by slug, so the venue must
exist before the event references it.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from engine.derive_helpers import (
    iso_utc_now, load_event, open_db, parse_event_uid_arg, record_activity,
)


_WORKER = "event-ship"

_ENTITY_TABLES = {
    "venue": ("venues", "venues"),
    "business": ("businesses", "businesses"),
    "community": ("communities", "communities"),
    "performer": ("performers", "performers"),
    "creator": ("creators", "creators"),
}
# Ordering matters — venue first, then hosts, then people. Event last.
_SHIP_ORDER = ("venue", "business", "community", "performer", "creator")


def _tl_configured() -> bool:
    return bool(
        os.environ.get("TOWNLOOPS_API_URL")
        and os.environ.get("TOWNLOOPS_API_KEY")
        and os.environ.get("TOWNLOOPS_TOWN_SLUG")
    )


def _slug_for(kind: str, entity_uid: str) -> str:
    town = os.environ.get("TOWNLOOPS_TOWN_SLUG", "unknown")
    return f"hermes-{town}-{entity_uid[:16]}"


def _venue_payload(row) -> dict:
    payload = {"name": row["name"]}
    for col in ("description", "address", "website", "phone",
                "image_alt", "category_slug"):
        if row[col]:
            payload[col] = row[col]
    return payload


def _community_payload(row) -> dict:
    payload = {"name": row["name"]}
    for col in ("kind", "description", "website", "meeting_info",
                "category_slug"):
        if row[col]:
            payload[col] = row[col]
    return payload


def _business_payload(row) -> dict:
    payload = {"name": row["name"]}
    for col in ("description", "address", "website", "phone", "category_slug",
                "instagram", "facebook", "tiktok", "twitter", "youtube",
                "linkedin"):
        if row[col]:
            payload[col] = row[col]
    return payload


def _performer_payload(row) -> dict:
    payload = {"name": row["name"]}
    for col in ("kind", "description", "website", "instagram",
                "spotify_url", "facebook", "tiktok", "twitter", "youtube",
                "linkedin"):
        if row[col]:
            payload[col] = row[col]
    return payload


def _creator_payload(row) -> dict:
    payload = {"name": row["name"]}
    for col in ("kind", "bio", "website", "instagram", "youtube_url",
                "facebook", "tiktok", "twitter", "linkedin"):
        if row[col]:
            payload[col] = row[col]
    return payload


_PAYLOAD_BUILDERS = {
    "venue": _venue_payload,
    "business": _business_payload,
    "community": _community_payload,
    "performer": _performer_payload,
    "creator": _creator_payload,
}


def _event_payload(row) -> dict:
    payload = {
        "title": row["title"] or row["name"],
        "start_at": row["date_utc"] or row["date"],
    }
    if row["description"]:
        payload["description"] = row["description"]
    if row["end_at_utc"] or row["end_at"]:
        payload["end_at"] = row["end_at_utc"] or row["end_at"]
    if row["venue_name"] or row["venue_raw"]:
        payload["venue_name"] = row["venue_name"] or row["venue_raw"]
    if row["category_slug"]:
        payload["category_slug"] = row["category_slug"]
    if row["source_url"]:
        payload["source_url"] = row["source_url"]
    return payload


def _ship_entity(client, conn, kind: str, entity_uid: str) -> Optional[str]:
    """POST or PATCH one entity. Returns TownLoops slug if successful,
    None if we couldn't ship."""
    table, resource = _ENTITY_TABLES[kind]
    row = conn.execute(
        f"SELECT * FROM {table} WHERE entity_uid = ?", (entity_uid,),
    ).fetchone()
    if not row:
        return None
    if row["source_kind"] == "live" and row["townloops_slug"]:
        # Local mirror of a row that already exists on TownLoops (staged by
        # event-reconcile) — nothing to create.
        return row["townloops_slug"]
    payload = _PAYLOAD_BUILDERS[kind](row)
    proposed_slug = row["townloops_slug"] or _slug_for(kind, entity_uid)
    payload["slug"] = proposed_slug
    idem = f"hermes-{os.environ.get('TOWNLOOPS_TOWN_SLUG','?')}:ship:{kind}:{entity_uid[:16]}"
    try:
        result = client.create(resource, payload, idempotency_key=idem)
    except Exception as e:
        # 409 slug_taken → already shipped (mirrors event-ingest behavior)
        msg = str(e)
        if "409" in msg or "slug_taken" in msg:
            slug = proposed_slug
        else:
            print(f"  ship {kind} {entity_uid[:12]} failed: {msg[:120]}",
                  file=sys.stderr)
            return None
    else:
        slug = (result or {}).get("slug", proposed_slug) if isinstance(result, dict) else proposed_slug
    now = iso_utc_now()
    conn.execute(
        f"UPDATE {table} SET townloops_slug = ?, pushed_at = ?, "
        f"  pushed_hash = COALESCE(content_hash, ?) WHERE entity_uid = ?",
        (slug, now, now, entity_uid),
    )
    return slug


def _ship_event(client, conn, event: dict, venue_slug: Optional[str]) -> Optional[str]:
    payload = _event_payload(event)
    if venue_slug:
        payload["venue_slug"] = venue_slug
    event_uid = event["event_uid"]
    proposed_slug = event.get("townloops_slug") or _slug_for("event", event_uid)
    payload["slug"] = proposed_slug
    idem = f"hermes-{os.environ.get('TOWNLOOPS_TOWN_SLUG','?')}:ship:event:{event_uid[:16]}"
    try:
        result = client.create("events", payload, idempotency_key=idem)
    except Exception as e:
        msg = str(e)
        if "409" in msg or "slug_taken" in msg:
            slug = proposed_slug
        else:
            print(f"ship event {event_uid[:12]} failed: {msg[:200]}", file=sys.stderr)
            return None
    else:
        slug = (result or {}).get("slug", proposed_slug) if isinstance(result, dict) else proposed_slug
    return slug


def _reconcile_change(event: dict) -> Optional[dict]:
    """event-reconcile metadata when this row is a change to a live event."""
    import json
    try:
        meta = (json.loads(event.get("extra_json") or "{}") or {}).get("reconcile") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    if meta.get("kind") == "changed" and meta.get("live_slug"):
        return meta
    return None


def _patch_live_event(client, conn, event: dict, meta: dict) -> Optional[str]:
    """Apply a source-side date/venue change to the existing live event."""
    import json
    changes = meta.get("changes") or {}
    patch: dict[str, Any] = {}
    if "start_at" in changes:
        patch["start_at"] = event["date_utc"] or changes["start_at"]["source"]
        if event["end_at_utc"] or event["end_at"]:
            patch["end_at"] = event["end_at_utc"] or event["end_at"]
    if "venue" in changes:
        patch["venue_name"] = event["venue_name"] or event["venue_raw"]
        if event.get("derived_venue_uid"):
            v = conn.execute(
                "SELECT extra_json FROM venues WHERE entity_uid = ?",
                (event["derived_venue_uid"],),
            ).fetchone()
            try:
                live_id = (json.loads(v["extra_json"] or "{}") if v else {}).get("live_id")
            except json.JSONDecodeError:
                live_id = None
            if live_id:
                patch["venue_id"] = live_id
    if not patch:
        return meta["live_slug"]
    try:
        client.update("events", meta["live_slug"], patch)
    except Exception as e:  # noqa: BLE001 — surfaced via rc=1 → workqueue retry
        print(f"patch event {meta['live_slug']} failed: {str(e)[:200]}", file=sys.stderr)
        return None
    return meta["live_slug"]


def _mark_shipped(conn, event_uid: str, slug: str, disabled: bool = False) -> None:
    now = iso_utc_now()
    conn.execute(
        "UPDATE events SET pushed_at = ?, townloops_slug = COALESCE(?, townloops_slug) "
        "WHERE event_uid = ?",
        (now, slug, event_uid),
    )
    record_activity(
        conn, _WORKER,
        "shipped-disabled" if disabled else "shipped",
        event_uid,
        detail=f"slug={slug}" + (" (TOWNLOOPS_API_KEY unset — local mark only)" if disabled else ""),
    )


def main() -> int:
    event_uid = parse_event_uid_arg()
    conn = open_db()

    event = load_event(conn, event_uid)
    if not event:
        print(f"event not found: {event_uid}", file=sys.stderr)
        return 2

    if not _tl_configured():
        _mark_shipped(conn, event_uid, _slug_for("event", event_uid), disabled=True)
        print(f"TOWNLOOPS_API_KEY unset — event {event_uid[:12]} marked shipped locally only")
        return 0

    from engine.townloops_api import Client, TownloopsError
    try:
        client = Client()
    except KeyError as e:
        # env var missing — treat as disabled
        _mark_shipped(conn, event_uid, _slug_for("event", event_uid), disabled=True)
        print(f"TownLoops env incomplete ({e}); marked local-only", file=sys.stderr)
        return 0

    # 1) Ship derived entities in order (venue → business → community →
    #    performer → creator).
    links = conn.execute(
        "SELECT entity_kind, entity_uid, role FROM linked_events WHERE event_uid = ?",
        (event_uid,),
    ).fetchall()
    by_kind: dict[str, list[dict]] = {k: [] for k in _SHIP_ORDER}
    for lk in links:
        k = lk["entity_kind"]
        if k in by_kind:
            by_kind[k].append(dict(lk))

    venue_slug: Optional[str] = None
    for kind in _SHIP_ORDER:
        for lk in by_kind[kind]:
            slug = _ship_entity(client, conn, kind, lk["entity_uid"])
            if slug and kind == "venue" and venue_slug is None:
                venue_slug = slug
            record_activity(
                conn, _WORKER, f"ship-{kind}", event_uid,
                detail=f"{lk['entity_uid'][:12]} → {slug or 'FAIL'}",
                ok=slug is not None,
                error=None if slug else "ship failed",
            )

    # 2) Ship the event itself — PATCH the live row if event-reconcile staged
    #    this as a change to an existing TownLoops event, else create.
    change = _reconcile_change(event)
    if change:
        event_slug = _patch_live_event(client, conn, event, change)
    else:
        event_slug = _ship_event(client, conn, event, venue_slug)
    if not event_slug:
        return 1

    _mark_shipped(conn, event_uid, event_slug, disabled=False)
    print(f"shipped event {event_uid[:12]} → {event_slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
