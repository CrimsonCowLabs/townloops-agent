#!/usr/bin/env python3
"""event-ship — POST one event + its derived entities to TownLoops.

Per plan adversarial pass #19: entities go first (venue, host, people),
then the event. The event references its venue by live `venue_id`, so the
venue must exist before the event references it. Nothing is created
without first looking it up live (`engine/live_lookup.py`).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from engine import categories as cats
from engine.derive_helpers import (
    iso_utc_now, load_event, open_db, parse_event_uid_arg, record_activity,
)
from engine.entity_ship import ship_entity as _ship_entity, slug_for as _slug_for
from engine.event_extract import town_tz_name, utc_times


_WORKER = "event-ship"

# Ordering matters — venue first, then hosts, then people. Event last.
_SHIP_ORDER = ("venue", "business", "community", "performer", "creator")


def _tl_configured() -> bool:
    return bool(
        os.environ.get("TOWNLOOPS_API_URL")
        and os.environ.get("TOWNLOOPS_API_KEY")
        and os.environ.get("TOWNLOOPS_TOWN_SLUG")
    )


def _event_classification(row) -> tuple[list[str], list[str]]:
    """(category_slugs, tags) to send: the row's categories mapped onto the
    town's live slugs (rows from before migration 017 or the old extraction
    vocabulary still carry e.g. `music`), and its tags as slugs."""
    stored = row.get("category_slugs") or row.get("category_slug")
    if stored and not cats.town_category_slugs():
        print(f"event {row['event_uid'][:12]}: no town categories snapshot "
              f"({cats.categories_snapshot_path()}) — shipping without categories; "
              "run `town-init setup`", file=sys.stderr)
    return cats.classify(stored, row.get("tags"))


def _event_payload(row) -> dict:
    """Times go out as UTC only — a naive local `date`/`end_at` is never sent.
    Categories go as `category_slugs` — create has no singular field."""
    payload = {
        "title": row["title"] or row["name"],
        "start_at": row["date_utc"],
    }
    if row["description"]:
        payload["description"] = row["description"]
    if row["end_at_utc"]:
        payload["end_at"] = row["end_at_utc"]
    if row["venue_name"] or row["venue_raw"]:
        payload["venue_name"] = row["venue_name"] or row["venue_raw"]
    category_slugs, tags = _event_classification(row)
    if category_slugs:
        payload["category_slugs"] = category_slugs
    if tags:
        payload["tags"] = tags
    if row["source_url"]:
        payload["source_url"] = row["source_url"]
    return payload




def _ship_event(client, conn, event: dict, venue_slug: Optional[str]) -> Optional[str]:
    """Create the event live — unless it is already there (by slug, or by
    source_url at the same time), in which case the live row is filled in
    instead. Several live rows for it park the event for the operator."""
    from engine.live_lookup import find_live_event, is_conflict
    payload = _event_payload(event)
    venue_id = _resolve_venue_id(client, conn, event, venue_slug)
    if venue_id:
        # By id: the server's name lookup fails outright on duplicate names.
        # The name rides along — unused when the id resolves, kept as the
        # curator's hint when it doesn't.
        payload["venue_id"] = venue_id
    event_uid = event["event_uid"]
    proposed_slug = event.get("townloops_slug") or _slug_for("event", event_uid)
    payload["slug"] = proposed_slug
    try:
        found = find_live_event(client, slug=proposed_slug, source_url=payload.get("source_url"),
                                start_at=payload["start_at"])
    except Exception as e:  # noqa: BLE001 — rc=1 → workqueue retry; never create blind
        msg = f"look up live event failed: {str(e)[:200]}"
        record_activity(conn, _WORKER, "find-live-event", event_uid, ok=False, error=msg)
        print(f"event {event_uid[:12]}: {msg}", file=sys.stderr)
        return None
    if found.ambiguous:
        slugs = ", ".join(sorted(r.get("slug") or "?" for r in found.ambiguous))
        _park_for_review(conn, event_uid, "find-live-event",
                         f"several live events share source_url {payload.get('source_url')} "
                         f"at this time: {slugs} — duplicate for the operator")
        return None
    if found.row:
        return _fill_live_event(client, conn, event, found.row, payload)

    idem = f"hermes-{os.environ.get('TOWNLOOPS_TOWN_SLUG','?')}:ship:event:{event_uid[:16]}"
    try:
        result = client.create("events", payload, idempotency_key=idem)
    except Exception as e:
        msg = str(e)
        if is_conflict(e):
            # Created since the lookup (another worker): reconcile to it.
            try:
                found = find_live_event(client, slug=proposed_slug, source_url=None, start_at=None)
            except Exception as e2:  # noqa: BLE001
                print(f"read live event {proposed_slug} failed: {str(e2)[:200]}", file=sys.stderr)
                return None
            if found.row:
                return _fill_live_event(client, conn, event, found.row, payload)
            msg = f"409 on create but no live event at {proposed_slug}: {msg}"
        elif "Multiple rows were found" in msg and "venue_id" not in payload:
            msg = (f"venue name {payload['venue_name']!r} matches several live venues — "
                   "derive/link the venue so the event can reference it by id")
        record_activity(conn, _WORKER, "ship-event", event_uid, ok=False, error=msg[:1000])
        print(f"ship event {event_uid[:12]} failed: {msg[:200]}", file=sys.stderr)
        return None
    slug = (result or {}).get("slug", proposed_slug) if isinstance(result, dict) else proposed_slug
    if isinstance(result, dict) and not _repair_dropped_sets(client, conn, event, slug, payload, result):
        return None
    return slug


def _fill_live_event(client, conn, event: dict, live: dict, payload: dict) -> Optional[str]:
    """Bring an event that is already live up to what we'd have created,
    without overriding anything on it: blank fields are filled, local
    categories/tags are added to the live sets, and a venue is bound only
    when it has none. Sent as a full-body PATCH and GET-verified; a mismatch
    parks the event for the operator. Returns the live slug."""
    from engine.live_patch import apply_event_patch
    slug = live["slug"]
    overrides = _fill_overrides(live, payload)
    if not overrides:
        return slug
    try:
        result = apply_event_patch(client, slug, overrides, current=live)
    except Exception as e:  # noqa: BLE001 — surfaced via rc=1 → workqueue retry
        print(f"patch live event {slug} failed: {str(e)[:200]}", file=sys.stderr)
        return None
    if result.mismatches:
        conn.execute("UPDATE events SET townloops_slug = ? WHERE event_uid = ?",
                     (slug, event["event_uid"]))
        _flag_for_review(conn, event["event_uid"], slug, result.mismatches)
        return None
    return slug


def _fill_overrides(live: dict, payload: dict) -> dict:
    out: dict[str, Any] = {}
    for k in ("title", "description", "start_at", "source_url"):
        if payload.get(k) and not live.get(k):
            out[k] = payload[k]
    # A source_url match may start at another time; our end only fits ours.
    if (payload.get("end_at") and not live.get("end_at")
            and _to_dt(payload["start_at"]) == _to_dt(live.get("start_at"))):
        out["end_at"] = payload["end_at"]
    for k, cap in (("category_slugs", cats.MAX_CATEGORIES), ("tags", cats.MAX_TAGS)):
        have = list(live.get(k) or [])
        merged = (have + [v for v in payload.get(k) or [] if v not in have])[:max(cap, len(have))]
        if merged != have:
            out[k] = merged
    if payload.get("venue_id") and not live.get("venue_id"):
        out["venue_id"] = payload["venue_id"]
    return out


def _repair_dropped_sets(client, conn, event: dict, slug: str, payload: dict,
                         created: dict) -> bool:
    """A create path has been seen to drop `tags` (CMO, bulk create). When the
    created row lacks a category/tag set that was sent, PATCH it in as a full
    body and GET-verify. False when it still doesn't stick — the event is then
    flagged for the operator, its live slug kept so nothing re-creates it."""
    from engine.live_patch import apply_event_patch
    dropped = {k: payload[k] for k in ("category_slugs", "tags")
               if payload.get(k) and sorted(payload[k]) != sorted(created.get(k) or [])}
    if not dropped:
        return True
    try:
        result = apply_event_patch(client, slug, dropped)
        mismatches = result.mismatches
    except Exception as e:  # noqa: BLE001 — flagged like a mismatch; a retry would only 409
        mismatches = [f"PATCH of {sorted(dropped)} failed: {str(e)[:200]}"]
    if not mismatches:
        return True
    conn.execute("UPDATE events SET townloops_slug = ? WHERE event_uid = ?",
                 (slug, event["event_uid"]))
    _flag_for_review(conn, event["event_uid"], slug, mismatches)
    return False


def _ensure_utc_times(conn, event: dict) -> bool:
    """Fill the UTC columns of a row queued before extraction set them,
    converting its local times in the town timezone. False when the start
    has no UTC form — the event must not ship."""
    if event.get("date_utc"):
        date_utc, end_at_utc = event["date_utc"], event.get("end_at_utc")
    else:
        date_utc, end_at_utc = utc_times(event.get("date"), event.get("end_at"))
    if not date_utc:
        return False
    if (date_utc, end_at_utc) != (event.get("date_utc"), event.get("end_at_utc")):
        conn.execute(
            "UPDATE events SET date_utc = ?, end_at_utc = ? WHERE event_uid = ?",
            (date_utc, end_at_utc, event["event_uid"]),
        )
        event["date_utc"], event["end_at_utc"] = date_utc, end_at_utc
    return True


def _reconcile_change(event: dict) -> Optional[dict]:
    """event-reconcile metadata when this row is a change to a live event."""
    import json
    try:
        meta = (json.loads(event.get("extra_json") or "{}") or {}).get("reconcile") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    if meta.get("kind") == "changed" and meta.get("live_slug"):
        return meta
    return None


def _to_dt(value: Optional[str]):
    from dateutil import tz as _tz
    from engine.reconcile import parse_start
    dt, _ = parse_start(value, _tz.gettz(town_tz_name()))
    return dt


def _patched_end(source_end: Optional[str], live_start: Optional[str],
                 live_end: Optional[str], new_start: str) -> Optional[str]:
    """End time to send with a date change: the source's own end if it is
    after the new start, else the live end shifted by the same amount as the
    start (keeps the duration). None when neither is usable — never an end
    that falls before the start."""
    start = _to_dt(new_start)
    if start is None:
        return None
    end = _to_dt(source_end)
    if end and end > start:
        return end.isoformat(timespec="seconds")
    ls, le = _to_dt(live_start), _to_dt(live_end)
    if ls and le and le > ls:
        return (start + (le - ls)).isoformat(timespec="seconds")
    return None


def _resolve_venue_id(client, conn, event: dict, venue_slug: Optional[str]) -> Optional[int]:
    """Live id of the event's venue: the derived venue's recorded live id
    (live mirror, or adopted/created by step 1 of main()), else its slug —
    or the step-1 slug when nothing was derived — looked up live. None when
    unresolved. Events bind venues by `venue_id`; `venue_slug` is ignored by
    the API and `venue_name` fails on duplicate names."""
    import json
    from engine.live_lookup import find_live_entity
    vuid = event.get("derived_venue_uid")
    if vuid:
        v = conn.execute(
            "SELECT extra_json, townloops_slug FROM venues WHERE entity_uid = ?", (vuid,),
        ).fetchone()
        if not v:
            return None
        try:
            live_id = (json.loads(v["extra_json"] or "{}") or {}).get("live_id")
        except (json.JSONDecodeError, AttributeError):
            live_id = None
        if live_id:
            return live_id
        venue_slug = v["townloops_slug"]
    if not venue_slug:
        return None
    try:
        return (find_live_entity(client, "venues", venue_slug) or {}).get("id")
    except Exception as e:  # noqa: BLE001 — treated as unresolved
        print(f"read live venue {venue_slug} failed: {str(e)[:200]}", file=sys.stderr)
        return None


def _flag_for_review(conn, event_uid: str, live_slug: str, mismatches: list[str]) -> None:
    """The PATCH landed but the live row doesn't show what was sent. Park the
    event as stuck for the operator and resolve its queue rows, so no retry
    PATCHes the live row again before a human has looked."""
    _park_for_review(conn, event_uid, "verify-live-event",
                     f"live event {live_slug} doesn't match the PATCH: " + "; ".join(mismatches))


def _park_for_review(conn, event_uid: str, action: str, msg: str) -> None:
    from engine import workqueue
    record_activity(conn, _WORKER, action, event_uid, ok=False, error=msg[:1000])
    workqueue.park(conn, event_uid)
    print(f"event {event_uid[:12]}: {msg[:400]}", file=sys.stderr)


def _patch_live_event(client, conn, event: dict, meta: dict,
                      venue_slug: Optional[str] = None) -> Optional[str]:
    """Apply a source-side date/venue change to the existing live event as a
    full-body PATCH built from the current live row, then GET-verify it.
    Returns the live slug, or None (→ rc 1) when the change can't be applied
    correctly or didn't land as sent (then the event is flagged stuck)."""
    from engine.live_patch import apply_event_patch
    changes = meta.get("changes") or {}
    live_slug = meta["live_slug"]
    if not ({"start_at", "venue"} & changes.keys()):
        return live_slug
    try:
        current = client.get("events", live_slug) or {}
    except Exception as e:  # noqa: BLE001 — surfaced via rc=1 → workqueue retry
        print(f"read live event {live_slug} failed: {str(e)[:200]}", file=sys.stderr)
        return None
    overrides: dict[str, Any] = {}
    if "start_at" in changes:
        overrides["start_at"] = event["date_utc"]
        overrides["end_at"] = _patched_end(event.get("end_at_utc"), current.get("start_at"),
                                           current.get("end_at"), event["date_utc"])
    if "venue" in changes:
        venue_id = _resolve_venue_id(client, conn, event, venue_slug)
        if not venue_id:
            print(f"patch event {live_slug}: new venue not resolved/shipped yet", file=sys.stderr)
            return None
        overrides["venue_id"] = venue_id
    try:
        result = apply_event_patch(client, live_slug, overrides, current=current)
    except Exception as e:  # noqa: BLE001 — surfaced via rc=1 → workqueue retry
        print(f"patch event {live_slug} failed: {str(e)[:200]}", file=sys.stderr)
        return None
    if result.mismatches:
        _flag_for_review(conn, event["event_uid"], live_slug, result.mismatches)
        return None
    return live_slug


def _supersede_older_copies(conn, event_uid: str, live_slug: str) -> None:
    """After a date change lands, the local row for the old date (a different
    uid) still maps to the same live event. Retire it so nothing reads a
    phantom occurrence or ships it back over the change."""
    conn.execute(
        "UPDATE events SET status = 'superseded', townloops_slug = NULL, "
        "  extra_json = CASE WHEN json_valid(COALESCE(extra_json, '{}')) "
        "    THEN json_set(COALESCE(extra_json, '{}'), '$.superseded_by', ?) ELSE extra_json END "
        "WHERE townloops_slug = ? AND event_uid != ?",
        (event_uid, live_slug, event_uid),
    )


def _mark_shipped(conn, event_uid: str, slug: str, disabled: bool = False) -> None:
    now = iso_utc_now()
    conn.execute(
        "UPDATE events SET pushed_at = ?, townloops_slug = COALESCE(?, townloops_slug) "
        "WHERE event_uid = ?",
        (now, slug, event_uid),
    )
    record_activity(
        conn, _WORKER,
        "shipped-disabled" if disabled else "shipped",
        event_uid,
        detail=f"slug={slug}" + (" (TOWNLOOPS_API_KEY unset — local mark only)" if disabled else ""),
    )


def main() -> int:
    event_uid = parse_event_uid_arg()
    conn = open_db()

    event = load_event(conn, event_uid)
    if not event:
        print(f"event not found: {event_uid}", file=sys.stderr)
        return 2

    if not _ensure_utc_times(conn, event):
        # rc 1 → workqueue retries, then marks the event stuck for the operator.
        msg = f"no UTC start time (date={event.get('date')!r}) — not shipped"
        record_activity(conn, _WORKER, "ship-event", event_uid, ok=False, error=msg)
        print(f"event {event_uid[:12]}: {msg}", file=sys.stderr)
        return 1

    if not _tl_configured():
        _mark_shipped(conn, event_uid, _slug_for("event", event_uid), disabled=True)
        print(f"TOWNLOOPS_API_KEY unset — event {event_uid[:12]} marked shipped locally only")
        return 0

    from engine.townloops_api import Client, TownloopsError
    try:
        client = Client()
    except KeyError as e:
        # env var missing — treat as disabled
        _mark_shipped(conn, event_uid, _slug_for("event", event_uid), disabled=True)
        print(f"TownLoops env incomplete ({e}); marked local-only", file=sys.stderr)
        return 0

    # 1) Ship derived entities in order (venue → business → community →
    #    performer → creator).
    links = conn.execute(
        "SELECT entity_kind, entity_uid, role FROM linked_events WHERE event_uid = ?",
        (event_uid,),
    ).fetchall()
    by_kind: dict[str, list[dict]] = {k: [] for k in _SHIP_ORDER}
    for lk in links:
        k = lk["entity_kind"]
        if k in by_kind:
            by_kind[k].append(dict(lk))

    venue_slug: Optional[str] = None
    for kind in _SHIP_ORDER:
        for lk in by_kind[kind]:
            slug = _ship_entity(client, conn, kind, lk["entity_uid"])
            if slug and kind == "venue" and venue_slug is None:
                venue_slug = slug
            record_activity(
                conn, _WORKER, f"ship-{kind}", event_uid,
                detail=f"{lk['entity_uid'][:12]} → {slug or 'FAIL'}",
                ok=slug is not None,
                error=None if slug else "ship failed",
            )

    # 2) Ship the event itself — PATCH the live row if event-reconcile staged
    #    this as a change to an existing TownLoops event, else create.
    change = _reconcile_change(event)
    if change:
        event_slug = _patch_live_event(client, conn, event, change, venue_slug=venue_slug)
        if event_slug:
            _supersede_older_copies(conn, event_uid, event_slug)
    else:
        event_slug = _ship_event(client, conn, event, venue_slug)
    if not event_slug:
        return 1

    _mark_shipped(conn, event_uid, event_slug, disabled=False)
    print(f"shipped event {event_uid[:12]} → {event_slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
