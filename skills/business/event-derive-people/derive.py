#!/usr/bin/env python3
"""event-derive-people — rule-based performer extraction. NULL is legit."""

from __future__ import annotations

import re
import sys

from engine.derive_helpers import (
    iso_utc_now, link_event_to_entity, load_event, mint_entity_uid,
    normalize_name, open_db, parse_event_uid_arg, record_activity,
)


_WORKER = "event-derive-people"

# "Sarah Smith at Alibi Ale Works" | "Live music: The Band" |
# "The Reggae Cowboys live @ Palisades" — extract the leading name up to
# the ` at ` / ` live ` / ` @ ` separator.
_PERFORMER_LEAD = re.compile(
    r"^(?P<name>[^\-—:]+?)\s+(?:at|live at|live @|@)\s+.+$",
    re.IGNORECASE,
)


def _find_performer_by_name(conn, name: str):
    n = normalize_name(name)
    if not n:
        return None
    for r in conn.execute("SELECT entity_uid, name FROM performers").fetchall():
        if normalize_name(r["name"]) == n:
            return dict(r)
    return None


def _insert_shell_performer(conn, event_uid: str, name: str) -> str:
    from hashlib import sha1 as _sha1
    entity_uid = mint_entity_uid("performer", name, event_uid)
    now = iso_utc_now()
    content_hash = _sha1(f"{name}|unknown".encode()).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO performers("
        "  entity_uid, source, source_kind, name, kind, "
        "  first_seen_at, last_seen_at, content_hash, last_modified_at, "
        "  derived_from_event_uid, derivation_confidence, "
        "  linked_event_count, enrichment_status"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending')",
        (entity_uid, f"derived:event:{event_uid[:12]}", "derived",
         name.strip(), "unknown",
         now, now, content_hash, now, event_uid, 60),
    )
    return entity_uid


def main() -> int:
    event_uid = parse_event_uid_arg()
    conn = open_db()

    event = load_event(conn, event_uid)
    if not event:
        print(f"event not found: {event_uid}", file=sys.stderr)
        return 2

    category = (event.get("category_slug") or "").lower()
    title = event.get("title") or event.get("name") or ""

    if category != "music":
        record_activity(conn, _WORKER, "skipped-non-music", event_uid,
                        detail=f"category={category}, no performer rule match")
        return 0

    m = _PERFORMER_LEAD.match(title)
    if not m:
        record_activity(conn, _WORKER, "no-performer-signal", event_uid,
                        detail=f"title '{title[:60]}' didn't match lead pattern")
        return 0

    performer_name = m.group("name").strip()
    if not performer_name or len(performer_name) > 80:
        return 0

    existing = _find_performer_by_name(conn, performer_name)
    if existing:
        entity_uid = existing["entity_uid"]
        record_activity(conn, _WORKER, "performer-matched", event_uid,
                        detail=f"matched '{performer_name}' → {entity_uid[:12]}")
    else:
        entity_uid = _insert_shell_performer(conn, event_uid, performer_name)
        record_activity(conn, _WORKER, "performer-created", event_uid,
                        detail=f"created shell performer '{performer_name}' → {entity_uid[:12]}")

    link_event_to_entity(conn, event_uid, "performer", entity_uid, role="performer")
    print(f"derived performer: {entity_uid} for event {event_uid[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
