#!/usr/bin/env python3
"""event-enrich-entities — research every entity linked to an event (venue,
business, community, performer, creator) from its own site and fill its
blank fields, under a per-event lookup budget (engine.entity_enrich).

Exits 0 whenever the event's entities were dealt with — including "not
found" and "budget ran out" — so a hard-to-find entity never parks the
event in stuck-people-derived."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

from engine import entity_enrich
from engine.derive_helpers import (
    load_event, open_db, parse_event_uid_arg, record_activity,
)
from engine.research import FirecrawlAuthError


_WORKER = "event-enrich-entities"
# Firecrawl searches + page reads per event; an event has ~1-4 entities and
# each costs 1-3 lookups.
_DEFAULT_MAX_LOOKUPS = 8


def _town_name() -> str:
    path = Path(os.environ.get("TOWN_PROFILE_PATH") or os.path.join(
        os.environ.get("HERMES_HOME", "/home/agent/.hermes"), "town-profile.yaml"))
    try:
        profile = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        profile = {}
    return profile.get("name") or os.environ.get("TOWNLOOPS_TOWN_SLUG", "")


def main() -> int:
    event_uid = parse_event_uid_arg()
    conn = open_db()

    event = load_event(conn, event_uid)
    if not event:
        print(f"event not found: {event_uid}", file=sys.stderr)
        return 2

    links = [(r["entity_kind"], r["entity_uid"]) for r in conn.execute(
        "SELECT DISTINCT entity_kind, entity_uid FROM linked_events WHERE event_uid = ? "
        "ORDER BY link_id", (event_uid,),
    ).fetchall() if r["entity_kind"] in entity_enrich.SPECS]

    try:
        max_lookups = int(os.environ.get("TOWNLOOPS_ENRICH_MAX_LOOKUPS", _DEFAULT_MAX_LOOKUPS))
    except ValueError:
        max_lookups = _DEFAULT_MAX_LOOKUPS
    budget = entity_enrich.Budget(max_lookups)
    town = _town_name()
    notes = []
    try:
        for kind, uid in links:
            out = entity_enrich.enrich_entity(conn, kind, uid, town, budget)
            note = f"{kind} {uid[:12]}: {out.status}"
            if out.filled:
                note += f" ({','.join(out.filled)})"
            notes.append(note)
            for err in out.errors:
                print(f"  dropped invalid value — {err}", file=sys.stderr)
        for kind, uid in links:
            if kind == "venue" and entity_enrich.borrow_property_image(conn, uid, links):
                notes.append(f"venue {uid[:12]}: property image")
    except FirecrawlAuthError as e:
        # Bad key: retrying every entity won't help — fail so the workqueue
        # retries and, if it persists, parks the event for the operator.
        record_activity(conn, _WORKER, "enriched", event_uid, ok=False, error=str(e)[:200])
        print(str(e), file=sys.stderr)
        return 1

    detail = "; ".join(notes) or "no linked entities"
    record_activity(conn, _WORKER, "enriched", event_uid, detail=detail)
    print(f"enriched event {event_uid[:12]} — {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
