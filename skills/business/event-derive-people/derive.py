#!/usr/bin/env python3
"""event-derive-people — the named performers and creators of an event, from
the same quote-checked AI pass as event-derive-hosts (engine/event_people,
cached on the event). NULL is legit. Each name becomes a shell performer or
creator for the performer-research batch, which alone decides who it is —
never inferred from the name."""

from __future__ import annotations

import sys

from engine import event_detail, event_people, workqueue
from engine.derive_helpers import (
    insert_entity, link_event_to_entity, load_event, mint_entity_uid,
    normalize_name, open_db, parse_event_uid_arg, record_activity,
)


_WORKER = "event-derive-people"
_ACTION = "extract-people"
_TABLES = {"performer": "performers", "creator": "creators"}


def _person(conn, event_uid: str, role: str, name: str) -> tuple[str, bool]:
    table = _TABLES[role]
    n = normalize_name(name)
    for r in conn.execute(f"SELECT entity_uid, name FROM {table}").fetchall():
        if normalize_name(r["name"]) == n:
            return r["entity_uid"], False
    entity_uid = mint_entity_uid(role, name, event_uid)
    insert_entity(conn, table, entity_uid, {
        "source": f"derived:event:{event_uid[:12]}", "source_kind": "derived", "name": name,
        "kind": "unknown" if role == "performer" else "other", "derived_from_event_uid": event_uid, "derivation_confidence": 60,
        "enrichment_status": "pending"})
    return entity_uid, True


def main() -> int:
    event_uid = parse_event_uid_arg()
    conn = open_db()

    event = load_event(conn, event_uid)
    if not event:
        print(f"event not found: {event_uid}", file=sys.stderr)
        return 2

    found = event_people.derive(conn, event, event_detail.cached_page(event_uid))
    if found is None:
        if workqueue.is_last_attempt(conn, event_uid, _ACTION):
            record_activity(conn, _WORKER, "no-performer-signal", event_uid,
                            detail="AI pass unavailable on the last attempt — no people",
                            ok=False, error="ai-unavailable")
            return 0
        print(f"event {event_uid[:12]}: people AI pass failed — retried", file=sys.stderr)
        return 1
    if not found["people"]:
        record_activity(conn, _WORKER, "no-performer-signal", event_uid,
                        detail="the event's text names no performer or creator")
        return 0
    for p in found["people"]:
        entity_uid, created = _person(conn, event_uid, p["role"], p["name"])
        link_event_to_entity(conn, event_uid, p["role"], entity_uid, role=p["role"])
        record_activity(conn, _WORKER, f"{p['role']}-{'created' if created else 'matched'}",
                        event_uid, detail=f"'{p['name']}' → {entity_uid[:12]}")
    print(f"derived {len(found['people'])} people for event {event_uid[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
