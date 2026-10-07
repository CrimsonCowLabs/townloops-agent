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
from engine.event_extract import town_tz_name, utc_times


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
    """Times go out as UTC only — a naive local `date`/`end_at` is never sent."""
    payload = {
        "title": row["title"] or row["name"],
        "start_at": row["date_utc"],
    }
    if row["description"]:
        payload["description"] = row["description"]
    if row["end_at_utc"]:
        payload["end_at"] = row["end_at_utc"]
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


def _ensure_utc_times(conn, event: dict) -> bool:
    """Fill the UTC columns of a row queued before extraction set them,
    converting its local times in the town timezone. False when the start
    has no UTC form — the event must not ship."""
    if event.get("date_utc"):
        date_utc, end_at_utc = event["date_utc"], event.get("end_at_utc")
    else:
        date_utc, end_at_utc = utc_times(event.get("date"), event.get("end_at"))
    if not date_utc:
        return False
    if (date_utc, end_at_utc) != (event.get("date_utc"), event.get("end_at_utc")):
        conn.execute(
            "UPDATE events SET date_utc = ?, end_at_utc = ? WHERE event_uid = ?",
            (date_utc, end_at_utc, event["event_uid"]),
        )
        event["date_utc"], event["end_at_utc"] = date_utc, end_at_utc
    return True


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


def _to_dt(value: Optional[str]):
    from dateutil import tz as _tz
    from engine.reconcile import parse_start
    dt, _ = parse_start(value, _tz.gettz(town_tz_name()))
    return dt


def _patched_end(source_end: Optional[str], live_start: Optional[str],
                 live_end: Optional[str], new_start: str) -> Optional[str]:
    """End time to send with a date change: the source's own end if it is
    after the new start, else the live end shifted by the same amount as the
    start (keeps the duration). None when neither is usable — never an end
    that falls before the start."""
    start = _to_dt(new_start)
    if start is None:
        return None
    end = _to_dt(source_end)
    if end and end > start:
        return end.isoformat(timespec="seconds")
    ls, le = _to_dt(live_start), _to_dt(live_end)
    if ls and le and le > ls:
        return (start + (le - ls)).isoformat(timespec="seconds")
    return None


def _venue_ref(conn, event: dict, venue_slug: Optional[str]) -> dict:
    """How to point the live event at its new venue: the live id of a live
    mirror, else the slug of the derived venue (shipped in step 1 of main()),
    else the step-1 slug when nothing was derived. {} when unresolved.
    A slug still has to become a `venue_id` — PATCH ignores `venue_slug`."""
    import json
    vuid = event.get("derived_venue_uid")
    if not vuid:
        return {"venue_slug": venue_slug} if venue_slug else {}
    v = conn.execute(
        "SELECT extra_json, townloops_slug FROM venues WHERE entity_uid = ?", (vuid,),
    ).fetchone()
    if not v:
        return {}
    try:
        live_id = json.loads(v["extra_json"] or "{}").get("live_id")
    except (json.JSONDecodeError, AttributeError):
        live_id = None
    if live_id:
        return {"venue_id": live_id}
    return {"venue_slug": v["townloops_slug"]} if v["townloops_slug"] else {}


def _flag_for_review(conn, event_uid: str, live_slug: str, mismatches: list[str]) -> None:
    """The PATCH landed but the live row doesn't show what was sent. Park the
    event as stuck for the operator and resolve its queue rows, so no retry
    PATCHes the live row again before a human has looked."""
    from engine import workqueue
    msg = f"live event {live_slug} doesn't match the PATCH: " + "; ".join(mismatches)
    record_activity(conn, _WORKER, "verify-live-event", event_uid, ok=False, error=msg[:1000])
    workqueue.park(conn, event_uid)
    print(f"event {event_uid[:12]}: {msg[:400]}", file=sys.stderr)


def _patch_live_event(client, conn, event: dict, meta: dict,
                      venue_slug: Optional[str] = None) -> Optional[str]:
    """Apply a source-side date/venue change to the existing live event as a
    full-body PATCH built from the current live row, then GET-verify it.
    Returns the live slug, or None (→ rc 1) when the change can't be applied
    correctly or didn't land as sent (then the event is flagged stuck)."""
    from engine.live_patch import apply_event_patch
    changes = meta.get("changes") or {}
    live_slug = meta["live_slug"]
    if not ({"start_at", "venue"} & changes.keys()):
        return live_slug
    try:
        current = client.get("events", live_slug) or {}
    except Exception as e:  # noqa: BLE001 — surfaced via rc=1 → workqueue retry
        print(f"read live event {live_slug} failed: {str(e)[:200]}", file=sys.stderr)
        return None
    overrides: dict[str, Any] = {}
    if "start_at" in changes:
        overrides["start_at"] = event["date_utc"]
        overrides["end_at"] = _patched_end(event.get("end_at_utc"), current.get("start_at"),
                                           current.get("end_at"), event["date_utc"])
    if "venue" in changes:
        ref = _venue_ref(conn, event, venue_slug)
        if "venue_slug" in ref:
            try:
                ref = {"venue_id": (client.get("venues", ref["venue_slug"]) or {}).get("id")}
            except Exception as e:  # noqa: BLE001
                print(f"read live venue {ref['venue_slug']} failed: {str(e)[:200]}", file=sys.stderr)
                ref = {}
        if not ref.get("venue_id"):
            print(f"patch event {live_slug}: new venue not resolved/shipped yet", file=sys.stderr)
            return None
        overrides.update(ref)
    try:
        result = apply_event_patch(client, live_slug, overrides, current=current)
    except Exception as e:  # noqa: BLE001 — surfaced via rc=1 → workqueue retry
        print(f"patch event {live_slug} failed: {str(e)[:200]}", file=sys.stderr)
        return None
    if result.mismatches:
        _flag_for_review(conn, event["event_uid"], live_slug, result.mismatches)
        return None
    return live_slug


def _supersede_older_copies(conn, event_uid: str, live_slug: str) -> None:
    """After a date change lands, the local row for the old date (a different
    uid) still maps to the same live event. Retire it so nothing reads a
    phantom occurrence or ships it back over the change."""
    conn.execute(
        "UPDATE events SET status = 'superseded', townloops_slug = NULL, "
        "  extra_json = CASE WHEN json_valid(COALESCE(extra_json, '{}')) "
        "    THEN json_set(COALESCE(extra_json, '{}'), '$.superseded_by', ?) ELSE extra_json END "
        "WHERE townloops_slug = ? AND event_uid != ?",
        (event_uid, live_slug, event_uid),
    )


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

    if not _ensure_utc_times(conn, event):
        # rc 1 → workqueue retries, then marks the event stuck for the operator.
        msg = f"no UTC start time (date={event.get('date')!r}) — not shipped"
        record_activity(conn, _WORKER, "ship-event", event_uid, ok=False, error=msg)
        print(f"event {event_uid[:12]}: {msg}", file=sys.stderr)
        return 1

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
        event_slug = _patch_live_event(client, conn, event, change, venue_slug=venue_slug)
        if event_slug:
            _supersede_older_copies(conn, event_uid, event_slug)
    else:
        event_slug = _ship_event(client, conn, event, venue_slug)
    if not event_slug:
        return 1

    _mark_shipped(conn, event_uid, event_slug, disabled=False)
    print(f"shipped event {event_uid[:12]} → {event_slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
