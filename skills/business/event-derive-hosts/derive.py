#!/usr/bin/env python3
"""event-derive-hosts — rule-based host identification. NULL is legit."""

from __future__ import annotations

import re
import sys

from engine.derive_helpers import (
    iso_utc_now, link_event_to_entity, load_event, mint_entity_uid,
    normalize_name, open_db, parse_event_uid_arg, record_activity,
)


_WORKER = "event-derive-hosts"

# Keywords that strongly suggest the event's title *is* the hosting
# community/civic body (rather than being an event AT some other venue).
_COMMUNITY_KEYWORDS = re.compile(
    r"\b(committee|council|board|foundation|library|church|parish|"
    r"congregation|society|association|club|coalition|"
    r"neighborhood|community|hoa)\b",
    re.IGNORECASE,
)
_MEETING_NOISE = re.compile(
    r"\s*(?:—|-|:)?\s*(meeting|monthly meeting|regular meeting|session|"
    r"gathering|assembly)\s*$",
    re.IGNORECASE,
)


def _trim_meeting_suffix(title: str) -> str:
    return _MEETING_NOISE.sub("", (title or "").strip()).strip()


def _find_community_by_name(conn, name: str):
    n = normalize_name(name)
    if not n:
        return None
    for r in conn.execute("SELECT entity_uid, name FROM communities").fetchall():
        if normalize_name(r["name"]) == n:
            return dict(r)
    return None


def _insert_shell_community(conn, event_uid: str, name: str) -> str:
    from hashlib import sha1 as _sha1
    entity_uid = mint_entity_uid("community", name, event_uid)
    now = iso_utc_now()
    content_hash = _sha1(f"{name}|civic".encode()).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO communities("
        "  entity_uid, source, source_kind, name, kind, "
        "  first_seen_at, last_seen_at, content_hash, last_modified_at, "
        "  derived_from_event_uid, derivation_confidence, linked_event_count"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (entity_uid, f"derived:event:{event_uid[:12]}", "derived",
         name.strip(), "civic",
         now, now, content_hash, now, event_uid, 70),
    )
    return entity_uid


def main() -> int:
    event_uid = parse_event_uid_arg()
    conn = open_db()

    event = load_event(conn, event_uid)
    if not event:
        print(f"event not found: {event_uid}", file=sys.stderr)
        return 2

    title = event.get("title") or event.get("name") or ""
    m = _COMMUNITY_KEYWORDS.search(title)
    if not m:
        record_activity(conn, _WORKER, "no-host-signal", event_uid,
                        detail=f"title '{title[:60]}' has no civic keyword")
        print(f"no host derivable from title; advancing NULL")
        return 0

    host_name = _trim_meeting_suffix(title)
    if not host_name:
        return 0

    existing = _find_community_by_name(conn, host_name)
    if existing:
        entity_uid = existing["entity_uid"]
        record_activity(conn, _WORKER, "host-matched", event_uid,
                        detail=f"matched community '{host_name}' → {entity_uid[:12]}")
    else:
        entity_uid = _insert_shell_community(conn, event_uid, host_name)
        record_activity(conn, _WORKER, "host-created", event_uid,
                        detail=f"created shell community '{host_name}' → {entity_uid[:12]}")

    conn.execute(
        "UPDATE events SET derived_host_uid = ?, derived_host_kind = 'community' "
        "WHERE event_uid = ?",
        (entity_uid, event_uid),
    )
    link_event_to_entity(conn, event_uid, "community", entity_uid, role="host")
    print(f"derived host (community): {entity_uid} for event {event_uid[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
