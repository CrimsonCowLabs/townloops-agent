#!/usr/bin/env python3
"""event-derive-hosts — the organisation hosting an event (a business, a
community group or a non-profit), from one quote-checked AI pass over the
event's own text (engine/event_people). NULL is legit: a name the text
doesn't carry, or a kind it doesn't support, means no host.

A failed AI call retries through the workqueue and, on the last attempt,
goes on with no host rather than parking the event."""

from __future__ import annotations

import sys

from engine import event_detail, event_people, workqueue
from engine.derive_helpers import (
    insert_entity, link_event_to_entity, load_event, mint_entity_uid,
    normalize_name, open_db, parse_event_uid_arg, record_activity,
)


_WORKER = "event-derive-hosts"
_ACTION = "extract-hosts"
_TABLES = {"business": "businesses", "community": "communities"}


def _find_by_name(conn, table: str, name: str):
    n = normalize_name(name)
    for r in conn.execute(f"SELECT entity_uid, name FROM {table}").fetchall():
        if normalize_name(r["name"]) == n:
            return dict(r)
    return None


def _host_entity(conn, event_uid: str, host: dict) -> tuple[str, str, bool]:
    """(entity kind, entity uid, created) for the verified host: a local row
    of the same name, else a new shell — a non-profit is a community with
    kind 'nonprofit'."""
    kind = "business" if host["kind"] == "business" else "community"
    table = _TABLES[kind]
    existing = _find_by_name(conn, table, host["name"])
    if existing:
        return kind, existing["entity_uid"], False
    entity_uid = mint_entity_uid(kind, host["name"], event_uid)
    fields = {"source": f"derived:event:{event_uid[:12]}", "source_kind": "derived",
              "name": host["name"], "derived_from_event_uid": event_uid,
              "derivation_confidence": 80}
    if kind == "community":
        fields["kind"] = "nonprofit" if host["kind"] == "nonprofit" else "group"
    insert_entity(conn, table, entity_uid, fields)
    return kind, entity_uid, True


def main() -> int:
    event_uid = parse_event_uid_arg()
    conn = open_db()

    event = load_event(conn, event_uid)
    if not event:
        print(f"event not found: {event_uid}", file=sys.stderr)
        return 2

    # event-reconcile may have already resolved the host business/community
    # from the source page's organizer field — honor it.
    preset, preset_kind = event.get("derived_host_uid"), event.get("derived_host_kind")
    preset_table = _TABLES.get(preset_kind or "")
    if preset and preset_table and conn.execute(
        f"SELECT 1 FROM {preset_table} WHERE entity_uid = ?", (preset,),
    ).fetchone():
        link_event_to_entity(conn, event_uid, preset_kind, preset, role="host")
        record_activity(conn, _WORKER, "host-preset", event_uid,
                        detail=f"kept pre-resolved {preset_kind} {preset[:12]}")
        print(f"derived host ({preset_kind}): {preset} for event {event_uid[:12]} (preset)")
        return 0

    found = event_people.derive(conn, event, event_detail.cached_page(event_uid))
    if found is None:
        if workqueue.is_last_attempt(conn, event_uid, _ACTION):
            record_activity(conn, _WORKER, "no-host-signal", event_uid,
                            detail="AI pass unavailable on the last attempt — no host",
                            ok=False, error="ai-unavailable")
            return 0
        print(f"event {event_uid[:12]}: host AI pass failed — retried", file=sys.stderr)
        return 1
    host = found["host"]
    if not host:
        record_activity(conn, _WORKER, "no-host-signal", event_uid,
                        detail="the event's text names no verifiable host")
        print("no host derivable; advancing NULL")
        return 0

    kind, entity_uid, created = _host_entity(conn, event_uid, host)
    record_activity(conn, _WORKER, "host-created" if created else "host-matched", event_uid,
                    detail=f"{host['kind']} '{host['name']}' → {entity_uid[:12]}")
    conn.execute("UPDATE events SET derived_host_uid = ?, derived_host_kind = ? "
                 "WHERE event_uid = ?", (entity_uid, kind, event_uid))
    link_event_to_entity(conn, event_uid, kind, entity_uid, role="host")
    print(f"derived host ({host['kind']}): {entity_uid} for event {event_uid[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
