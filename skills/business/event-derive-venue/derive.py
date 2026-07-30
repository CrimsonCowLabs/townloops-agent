#!/usr/bin/env python3
"""event-derive-venue — match or create venue for an approved event."""

from __future__ import annotations

import sys

from engine.derive_helpers import (
    iso_utc_now, link_event_to_entity, load_event, mint_entity_uid,
    normalize_name, open_db, parse_event_uid_arg, record_activity,
)


_WORKER = "event-derive-venue"


def _find_venue_by_name(conn, name: str):
    if not name:
        return None
    n = normalize_name(name)
    if not n:
        return None
    rows = conn.execute(
        "SELECT entity_uid, name FROM venues"
    ).fetchall()
    for r in rows:
        if normalize_name(r["name"]) == n:
            return dict(r)
    return None


def _insert_shell_venue(conn, event_uid: str, name: str,
                        location: str, category: str) -> str:
    entity_uid = mint_entity_uid("venue", name, event_uid)
    now = iso_utc_now()
    # NOT NULL: content_hash, last_modified_at. Silent INSERT OR IGNORE
    # would swallow the constraint failure — populate them explicitly.
    from hashlib import sha1 as _sha1
    content_hash = _sha1(f"{name}|{location or ''}|{category or ''}".encode()).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO venues("
        "  entity_uid, source, source_kind, name, address, category_slug, "
        "  first_seen_at, last_seen_at, content_hash, last_modified_at, "
        "  derived_from_event_uid, derivation_confidence, "
        "  linked_event_count, enrichment_status"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending')",
        (entity_uid, f"derived:event:{event_uid[:12]}", "derived",
         name.strip(), location or None, category or None,
         now, now, content_hash, now, event_uid, 80),
    )
    return entity_uid


def main() -> int:
    event_uid = parse_event_uid_arg()
    conn = open_db()

    event = load_event(conn, event_uid)
    if not event:
        print(f"event not found: {event_uid}", file=sys.stderr)
        return 2

    venue_name = (event.get("venue_raw") or event.get("venue_name")
                  or event.get("hosting_group") or "").strip()
    if not venue_name:
        record_activity(conn, _WORKER, "no-venue-signal", event_uid,
                        detail="no venue_raw/venue_name/hosting_group",
                        ok=False, error="missing venue signal")
        print(f"no venue signal for event {event_uid}", file=sys.stderr)
        return 2

    existing = _find_venue_by_name(conn, venue_name)
    if existing:
        entity_uid = existing["entity_uid"]
        record_activity(conn, _WORKER, "venue-matched", event_uid,
                        detail=f"matched existing '{existing['name']}' → {entity_uid[:12]}")
    else:
        entity_uid = _insert_shell_venue(
            conn, event_uid, venue_name,
            event.get("location") or event.get("location_raw") or "",
            event.get("category_slug") or "",
        )
        record_activity(conn, _WORKER, "venue-created", event_uid,
                        detail=f"created shell venue '{venue_name}' → {entity_uid[:12]}")

    conn.execute(
        "UPDATE events SET derived_venue_uid = ? WHERE event_uid = ?",
        (entity_uid, event_uid),
    )
    link_event_to_entity(conn, event_uid, "venue", entity_uid, role="primary")
    print(f"derived venue: {entity_uid} for event {event_uid[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
