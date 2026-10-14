#!/usr/bin/env python3
"""event-enrich-entities — research every venue, business and community
linked to an event from its own site and fill its blank fields, under a
per-event lookup budget (engine.entity_enrich), then give any still without
a category one from the town's vocabulary (engine.entity_classify). Performers and creators are
left to the scheduled performer-research batch (engine.performer_research).

Exits 0 whenever the event's entities were dealt with — including "not
found" and "budget ran out" — so a hard-to-find entity never parks the
event in stuck-people-derived."""

from __future__ import annotations

import sys
import time

from engine import entity_classify, entity_enrich, performer_research
from engine.derive_helpers import (
    load_event, open_db, parse_event_uid_arg, record_activity, town_name,
)
from engine.research import FirecrawlAuthError


_WORKER = "event-enrich-entities"
# Firecrawl searches + page reads per event; an event has ~1-4 entities and
# each costs 1-3 lookups.
_DEFAULT_MAX_LOOKUPS = 8
# Codex calls — a community's or venue's site read for its meeting info or
# accessibility notes, and categorizing entities the keyword map can't
# place — capped, and none started that could end past _LLM_DEADLINE_S into
# the run, so the skill stays inside the orchestrator's 120s subprocess
# timeout.
_MAX_LLM_CALLS = 3
_LLM_TIMEOUT_S = 30
_LLM_DEADLINE_S = 100


def main() -> int:
    started = time.monotonic()
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

    budget = entity_enrich.Budget.from_env(_DEFAULT_MAX_LOOKUPS)
    llm = entity_classify.codex_llm(_MAX_LLM_CALLS, _LLM_TIMEOUT_S, started + _LLM_DEADLINE_S)
    town = town_name()
    notes = []
    try:
        for kind, uid in links:
            if kind in performer_research.KINDS:
                # A name search can't tell a local act from a namesake; the
                # scheduled performer-research batch walks the evidence ladder.
                notes.append(f"{kind} {uid[:12]}: left for performer-research")
                continue
            out = entity_enrich.enrich_entity(conn, kind, uid, town, budget, llm)
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
