#!/usr/bin/env python3
"""event-enrich-entities — v1 pass-through. Marks derived entities as
'attempted' and advances the event state. Real LLM+Firecrawl enrichment
lives in a follow-up milestone."""

from __future__ import annotations

import sys

from engine.derive_helpers import (
    load_event, open_db, parse_event_uid_arg, record_activity,
)


_WORKER = "event-enrich-entities"

_ENTITY_TABLES = {
    "venue": "venues",
    "business": "businesses",
    "community": "communities",
    "performer": "performers",
    "creator": "creators",
}


def _mark_attempted(conn, entity_uid: str, table: str) -> None:
    # Only venues/businesses/performers have enrichment_status columns
    # (communities/creators don't per current schema). Guard so this skill
    # works uniformly across the 5 kinds.
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info('{table}')")}
    if "enrichment_status" in cols:
        conn.execute(
            f"UPDATE {table} SET enrichment_status = COALESCE(enrichment_status, 'attempted') "
            f"WHERE entity_uid = ?", (entity_uid,),
        )


def main() -> int:
    event_uid = parse_event_uid_arg()
    conn = open_db()

    event = load_event(conn, event_uid)
    if not event:
        print(f"event not found: {event_uid}", file=sys.stderr)
        return 2

    links = conn.execute(
        "SELECT entity_kind, entity_uid FROM linked_events WHERE event_uid = ?",
        (event_uid,),
    ).fetchall()

    n_marked = 0
    for lk in links:
        table = _ENTITY_TABLES.get(lk["entity_kind"])
        if not table:
            continue
        _mark_attempted(conn, lk["entity_uid"], table)
        n_marked += 1

    record_activity(conn, _WORKER, "pass-through", event_uid,
                    detail=f"marked {n_marked} linked entities as attempted (v1 no-op)")
    print(f"pass-through enrichment for event {event_uid[:12]} — {n_marked} entities marked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
