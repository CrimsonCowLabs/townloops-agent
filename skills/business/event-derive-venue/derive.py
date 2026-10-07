#!/usr/bin/env python3
"""event-derive-venue — match or create venue for an approved event."""

from __future__ import annotations

import os
import sys
from typing import Callable, Iterable, NamedTuple, Optional

from engine import workqueue
from engine.derive_helpers import (
    entity_richness, iso_utc_now, link_event_to_entity, load_event,
    load_live_venues, match_live_venue, mint_entity_uid, mirror_live_entity,
    normalize_name, open_db, parse_event_uid_arg, record_activity,
)


_WORKER = "event-derive-venue"


def _find_venue_by_name(conn, name: str):
    """Local venue with this exact normalized name; one already on TownLoops
    (townloops_slug set) beats an unshipped shell."""
    n = normalize_name(name)
    if not n:
        return None
    rows = [dict(r) for r in conn.execute("SELECT * FROM venues").fetchall()
            if normalize_name(r["name"]) == n]
    if not rows:
        return None
    return max(rows, key=lambda r: (bool(r.get("townloops_slug")), entity_richness(r)))


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


class Outcome(NamedTuple):
    action: str                     # activity action; "venue-review" means parked
    entity_uid: Optional[str] = None
    detail: str = ""


def _venue_name(event: dict) -> str:
    return (event.get("venue_raw") or event.get("venue_name")
            or event.get("hosting_group") or "").strip()


def derive_venue(conn, event: dict,
                 live_venues: Callable[[], Iterable[dict]]) -> Outcome:
    """Bind the event to a venue: a local row already on TownLoops, else a live
    TownLoops venue (mirrored locally so ship never POSTs it again), else an
    unshipped local row, else a new shell. A guess at a live venue parks the
    event for the operator — even over an unshipped local row, which would
    ship as the same duplicate.

    Only the live *venue* list is consulted — a business with the same name is
    not a venue and can't bind an event (CMO: cuppa-business-vs-venue)."""
    venue_name = _venue_name(event)
    event_uid = event["event_uid"]
    local = _find_venue_by_name(conn, venue_name)
    if not (local and local.get("townloops_slug")):
        m = match_live_venue(venue_name, live_venues())
        if m.kind == "match":
            entity_uid = mirror_live_entity(conn, "venue", m.venue)
            return _bind(conn, event_uid, entity_uid, "venue-matched-live",
                         f"'{venue_name}' → live venue {m.venue['slug']}")
        if m.kind == "review":
            workqueue.park(conn, event_uid)
            names = ", ".join(f"{c['name']} ({c['slug']})" for c in m.candidates[:5])
            return Outcome("venue-review", None,
                           f"'{venue_name}' may be live venue {names} — set venue_raw to the "
                           "exact live name, or to a distinct name for a new venue, then unstick")
    if local:
        return _bind(conn, event_uid, local["entity_uid"], "venue-matched",
                     f"matched existing '{local['name']}' → {local['entity_uid'][:12]}")

    entity_uid = _insert_shell_venue(
        conn, event_uid, venue_name,
        event.get("location") or event.get("location_raw") or "",
        event.get("category_slug") or "",
    )
    return _bind(conn, event_uid, entity_uid, "venue-created",
                 f"created shell venue '{venue_name}' → {entity_uid[:12]}")


def _bind(conn, event_uid: str, entity_uid: str, action: str, detail: str) -> Outcome:
    conn.execute(
        "UPDATE events SET derived_venue_uid = ? WHERE event_uid = ?",
        (entity_uid, event_uid),
    )
    link_event_to_entity(conn, event_uid, "venue", entity_uid, role="primary")
    return Outcome(action, entity_uid, detail)


class LiveLookupError(Exception):
    pass


def _fetch_live_venues() -> list[dict]:
    """This town's live venues (the venues endpoint only — not businesses)."""
    from engine.townloops_api import Client
    client = Client()
    client.verify_town()
    return list(client.paginate("venues"))


def main() -> int:
    event_uid = parse_event_uid_arg()
    conn = open_db()

    event = load_event(conn, event_uid)
    if not event:
        print(f"event not found: {event_uid}", file=sys.stderr)
        return 2

    # event-reconcile may have already resolved the venue (incl. matching a
    # live TownLoops venue whose name differs from venue_raw) — honor it.
    preset = event.get("derived_venue_uid")
    if preset and conn.execute(
        "SELECT 1 FROM venues WHERE entity_uid = ?", (preset,),
    ).fetchone():
        link_event_to_entity(conn, event_uid, "venue", preset, role="primary")
        record_activity(conn, _WORKER, "venue-preset", event_uid,
                        detail=f"kept pre-resolved venue {preset[:12]}")
        print(f"derived venue: {preset} for event {event_uid[:12]} (preset)")
        return 0

    if not _venue_name(event):
        record_activity(conn, _WORKER, "no-venue-signal", event_uid,
                        detail="no venue_raw/venue_name/hosting_group",
                        ok=False, error="missing venue signal")
        print(f"no venue signal for event {event_uid}", file=sys.stderr)
        return 2

    def live_venues() -> list[dict]:
        # Unconfigured = local-only mode (ship won't POST either); not cached,
        # so adding the key takes effect on the next event.
        if not os.environ.get("TOWNLOOPS_API_KEY"):
            return []
        try:
            return load_live_venues(_fetch_live_venues)
        except Exception as e:  # noqa: BLE001 — API/network down: retry, don't guess
            raise LiveLookupError(str(e)[:300]) from e

    try:
        out = derive_venue(conn, event, live_venues)
    except LiveLookupError as e:
        msg = f"live venue lookup failed: {e}"
        record_activity(conn, _WORKER, "venue-live-lookup", event_uid, ok=False, error=msg)
        print(msg, file=sys.stderr)
        return 1

    if out.action == "venue-review":
        # Parked (stuck-) for the operator; nonzero so the drainer doesn't
        # advance it. The drainer then calls workqueue.fail on the already-
        # resolved row: that only records stderr as last_error (where the
        # operator sees the candidates) — mark_stuck is a no-op when stuck.
        record_activity(conn, _WORKER, out.action, event_uid, detail=out.detail,
                        ok=False, error="ambiguous live venue match")
        print(out.detail, file=sys.stderr)
        return 1

    record_activity(conn, _WORKER, out.action, event_uid, detail=out.detail)
    print(f"derived venue: {out.entity_uid} for event {event_uid[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
