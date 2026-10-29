"""Make sure one local entity exists on TownLoops and reflects our values —
the one entity ship path, used by event-ship (an event's venue, hosts and
people) and by business-discovery's promote (an operator-approved business).

Nothing is created without first looking it up live (engine/live_lookup):
by its Ingest Ref first (engine/ingest_ref — survives a wiped local cache and
a TownLoops-side rename), then, for rows shipped before refs, by the cached
slug. A create never proposes a slug — TownLoops derives it — and carries the
ref; a `409 ingest_ref_taken` names the row that already holds it, which is
bound with no second lookup. A created, bound or matched row is then synced
(engine/entity_sync).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Optional

from engine.derive_helpers import iso_utc_now, parse_extra_json, record_activity


ENTITY_TABLES = {
    "venue": ("venues", "venues"),
    "business": ("businesses", "businesses"),
    "community": ("communities", "communities"),
    "performer": ("performers", "performers"),
    "creator": ("creators", "creators"),
}


class AwaitingResearch(RuntimeError):
    """A bare performer/creator research hasn't verified: not created live
    (engine/performer_research.awaits_research). A hold, not a failure."""

    def __init__(self, kind: str, status: Optional[str]):
        self.reason = f"awaiting research: {status or 'pending'}"
        super().__init__(f"{kind} {self.reason}")


def _venue_payload(row) -> dict:
    payload = {"name": row["name"]}
    for col in ("description", "address", "website", "phone",
                "accessibility_notes", "image_alt", "category_slug"):
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


PAYLOAD_BUILDERS = {
    "venue": _venue_payload,
    "business": _business_payload,
    "community": _community_payload,
    "performer": _performer_payload,
    "creator": _creator_payload,
}


def ship_entity(client, conn, kind: str, entity_uid: str) -> Optional[str]:
    """Make sure one entity exists live and reflects our local values.
    Returns its TownLoops slug, or None if we couldn't ship it.

    Looked up by its Ingest Ref, else (shipped before refs) at its cached
    slug — or, with none cached, the slug pre-ref creates proposed, which is
    the ref's own string. A row found under another slug than cached (renamed
    or merged on TownLoops) is re-pointed (`entity_sync.repoint`); one found
    by slug without our ref is given it (`ingest_ref.adopt`, set-once PATCH).
    Not found: created with the ref and no slug; a `409 ingest_ref_taken`
    binds to the row it names, with no second lookup. A shipped row gone from
    live (no ref match, cached slug 404s with no survivor) is flagged for the
    operator, not re-created. The bound row is then synced
    (engine/entity_sync): live values kept, live blanks filled, GET-verified.
    A create the submission gate holds (autonomous mode) raises
    autonomy.SubmissionsHeld, and a never-shipped bare performer/creator
    research hasn't verified raises AwaitingResearch before any lookup —
    holds, not failures; the caller decides."""
    from engine import entity_sync
    from engine.autonomy import SubmissionsHeld
    from engine.ingest_ref import ref_for, remember, taken_slug
    from engine.live_lookup import find_live_entity
    from engine.performer_research import awaits_research
    table, resource = ENTITY_TABLES[kind]
    row = conn.execute(
        f"SELECT * FROM {table} WHERE entity_uid = ?", (entity_uid,),
    ).fetchone()
    if not row:
        return None
    if row["source_kind"] == "live" and row["townloops_slug"]:
        # Local mirror of a row that already exists on TownLoops (staged by
        # event-reconcile) — nothing to create.
        return row["townloops_slug"]
    if row["townloops_slug"] and entity_sync.is_cross_town(dict(row)):
        # Filed under another town: our town-scoped GET 404s; nothing to do.
        return row["townloops_slug"]
    if not row["pushed_at"] and awaits_research(kind, dict(row)):
        raise AwaitingResearch(kind, row["enrichment_status"])
    ref = ref_for(entity_uid)
    cached = row["townloops_slug"]
    created = bound = False
    try:
        live = find_live_entity(client, resource, cached or ref, ingest_ref=ref)
        if live is None and row["pushed_at"] and cached:
            entity_sync.flag_for_operator(conn, kind, dict(row), "missing",
                                          f"live {kind} {cached} not found (404, "
                                          f"no row carries {ref}) — not re-created")
            return None
        if live is None:
            payload = PAYLOAD_BUILDERS[kind](row)
            payload["ingest_ref"] = ref
            idem = f"hermes-{os.environ.get('TOWNLOOPS_TOWN_SLUG','?')}:ship:{kind}:{entity_uid[:16]}"
            try:
                live, created = client.create(resource, payload, idempotency_key=idem), True
            except Exception as e:
                taken = taken_slug(e)
                if not taken:
                    raise
                # Already shipped (a wiped cache, a lost response): bind to it.
                live, bound = {"slug": taken}, True
    except SubmissionsHeld:
        raise
    except Exception as e:  # noqa: BLE001 — surfaced as a failed ship-<kind> activity
        print(f"  ship {kind} {entity_uid[:12]} failed: {str(e)[:120]}", file=sys.stderr)
        return None
    live = live if isinstance(live, dict) else {}
    slug = live.get("slug")
    if not slug:
        print(f"  ship {kind} {entity_uid[:12]} failed: no slug in the answer", file=sys.stderr)
        return None
    town = os.environ.get("TOWNLOOPS_TOWN_SLUG")
    # The server may file a new venue under another (ghost) town, e.g. a Reno
    # venue. Events resolve `venue_id` in-town only, so such an id would
    # silently unbind the event — leave it unrecorded and bind by name.
    in_town = live.get("town_slug") in (None, "", town)
    if cached and slug != cached and in_town:
        # Renamed (or merged) on TownLoops: the ref, or the merge, says where.
        entity_sync.repoint(conn, kind, dict(row), live)
        row = conn.execute(f"SELECT * FROM {table} WHERE entity_uid = ?",
                           (entity_uid,)).fetchone()
    if created or bound or live.get("ingest_ref") == ref:
        remember(conn, kind, entity_uid, ref)
    elif in_town and row["ingest_ref"] != ref:
        _adopt_ref(client, conn, kind, entity_uid, resource, live, ref)
    _record_live_entity(conn, table, row, slug, live.get("id") if in_town else None,
                        None if in_town else live.get("town_slug"))
    if in_town:
        res = entity_sync.sync_entity(client, conn, kind, entity_uid)
        if res.status == "error":
            print(f"  sync {kind} {entity_uid[:12]} → {slug}: {res.detail}", file=sys.stderr)
    return slug


def _adopt_ref(client, conn, kind: str, entity_uid: str, resource: str, live: dict,
               ref: str) -> None:
    """A row shipped before refs, found by slug: give it our ref (set-once
    PATCH) and remember it. A failure is recorded and retried next ship."""
    from engine.ingest_ref import adopt, remember
    result = adopt(client, resource, live, ref)
    if result.status in ("adopted", "already"):
        remember(conn, kind, entity_uid, ref)
    if result.status != "already":
        record_activity(conn, "entity-sync", "adopt-ref", f"{kind}:{entity_uid}",
                        detail=f"{live.get('slug')}: {result.status} {result.detail}".strip(),
                        ok=result.status == "adopted",
                        error=None if result.status == "adopted" else result.detail or result.status)


def ship_released(client, conn, limit: int = 25,
                  kinds: Optional[tuple[str, ...]] = None) -> dict[str, int]:
    """Ship the performers/creators event-ship held back for research
    (AwaitingResearch) that research — or the operator — has since
    released, when an event they're linked to is already live (an event
    not live yet ships them itself, before it). Each goes through
    ship_entity; its local event links stand. A failed one is stamped
    like a failed sync (entity_sync.note_error) and retried after the rest.
    In autonomous mode `client` is a GatedClient: a held gate stops the
    pass until the next run. `kinds` narrows it (others are ignored).
    Returns a count per outcome (shipped / failed / held)."""
    from collections import Counter
    from engine import entity_sync
    from engine.autonomy import SubmissionsHeld
    from engine.performer_research import KINDS, awaits_research
    kinds = tuple(k for k in KINDS if kinds is None or k in kinds)
    if not kinds:
        return {}
    client.verify_town()
    counts: Counter[str] = Counter()
    for kind in kinds:
        table, _ = ENTITY_TABLES[kind]
        rows = conn.execute(
            f"SELECT t.*, group_concat(DISTINCT e.townloops_slug) AS live_events "
            f"FROM {table} t "
            "JOIN linked_events l ON l.entity_kind = ? AND l.entity_uid = t.entity_uid "
            "JOIN events e ON e.event_uid = l.event_uid "
            "WHERE t.pushed_at IS NULL AND COALESCE(t.source_kind, '') != 'live' "
            "  AND e.pushed_at IS NOT NULL AND e.townloops_slug IS NOT NULL "
            f"GROUP BY t.entity_uid ORDER BY {entity_sync.failed_last('t')}",
            (kind,)).fetchall()
        for row in rows:
            if limit <= 0:
                return dict(counts)
            if awaits_research(kind, dict(row)):
                continue
            limit -= 1
            uid = row["entity_uid"]
            try:
                slug = ship_entity(client, conn, kind, uid)
            except SubmissionsHeld as e:
                counts["held"] += 1
                print(f"  ship {kind} {uid[:12]}: submissions held ({e.reason})",
                      file=sys.stderr)
                return dict(counts)
            except Exception as e:  # noqa: BLE001 — counted as failed, retried later
                print(f"  ship {kind} {uid[:12]} failed: {str(e)[:120]}", file=sys.stderr)
                slug = None
            if not slug:
                entity_sync.note_error(conn, kind, uid)
            counts["shipped" if slug else "failed"] += 1
            record_activity(conn, "entity-sync", f"ship-{kind}", f"{kind}:{uid}",
                            detail=f"research resolved → {slug or 'FAIL'}; "
                                   f"its live events: {row['live_events']}",
                            ok=slug is not None, error=None if slug else "ship failed")
            conn.commit()
    return dict(counts)


def cross_town(conn, kind: str, entity_uid: str) -> Optional[str]:
    """The other town the server filed this shipped entity under, if any."""
    table, _ = ENTITY_TABLES[kind]
    row = conn.execute(f"SELECT extra_json FROM {table} WHERE entity_uid = ?",
                       (entity_uid,)).fetchone()
    return parse_extra_json(row and row[0]).get("cross_town")


def _record_live_entity(conn, table: str, row, slug: str, live_id: Optional[int],
                        cross_town: Optional[str]) -> None:
    """Record the live slug/id, or the other town the server filed it under
    (entity_sync then leaves it alone). pushed_hash is entity_sync's to set,
    after a GET verify."""
    extra = parse_extra_json(row["extra_json"])
    if live_id is not None:
        extra["live_id"] = live_id
    if cross_town:
        extra["cross_town"] = cross_town
    conn.execute(
        f"UPDATE {table} SET townloops_slug = ?, pushed_at = ?, extra_json = ? "
        f"WHERE entity_uid = ?",
        (slug, iso_utc_now(), json.dumps(extra) if extra else row["extra_json"],
         row["entity_uid"]),
    )
