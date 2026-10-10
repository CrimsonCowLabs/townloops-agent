#!/usr/bin/env python3
"""event-ship — POST one event + its derived entities to TownLoops.

Per plan adversarial pass #19: entities go first (venue, host, people),
then the event. The event references its venue by live `venue_id`, so the
venue must exist before the event references it. Nothing is created
without first looking it up live (`engine/live_lookup.py`).

TownLoops Series (ADR 0007, `engine/series.py`): an event joins a Series
only by TownLoops auto-linking its title at create, so the title it ships
under is decided here (`series.decide_title`, recorded as a `series-title`
activity; the local title is never changed). The Series it will join makes
the live lookup's strongest check — an occurrence of it on the same
town-local date is this event. After shipping, the Series TownLoops reports
is mirrored locally and blank Series-shared fields fill from siblings.

Autonomous mode (engine/autonomy.py): the client is a GatedClient — a create
waits (rc 75, the workqueue releases the claim without spending an attempt)
while submissions are paused, the circuit is open or the pending cap is
reached, and must come back `pending`. A live match TownLoops rejected means
"don't submit" (the event is declined locally); a pending one is adopted
and filled. A reconcile-staged change to a published value (date, venue or
any other field) is submitted as a pending event carrying its source_url, for
the TownLoops merge flow, never PATCHed; a change that only fills a blank live
field is PATCHed live like any fill-blank. Blank fields are first filled from the best other copy of the event
(engine/event_dedup.best_of_copies).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from engine import autonomy
from engine import categories as cats
from engine import event_dedup, event_detail, event_state, event_tickets
from engine.derive_helpers import (
    iso_utc_now, load_event, open_db, parse_event_uid_arg, record_activity,
)
from engine.entity_ship import ship_entity as _ship_entity, slug_for as _slug_for
from engine.event_extract import town_tz_name, utc_times


_WORKER = "event-ship"
# Exit code for "not now": the workqueue releases the claim without counting
# an attempt (engine/orchestrator).
RC_DEFERRED = 75
# _ship_event's answer when TownLoops already rejected this event.
_DECLINED = object()

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
    vocabulary still carry e.g. `music`), and its tags as slugs — `free`
    added for a stated-free event, dropped for one with a stated price."""
    stored = row.get("category_slugs") or row.get("category_slug")
    if stored and not cats.town_category_slugs():
        print(f"event {row['event_uid'][:12]}: no town categories snapshot "
              f"({cats.categories_snapshot_path()}) — shipping without categories; "
              "run `town-init setup`", file=sys.stderr)
    category_slugs, tags = cats.classify(stored, row.get("tags"))
    return category_slugs, event_tickets.ticket_tags(tags, row.get("price"))


def _ticket_line(row) -> Optional[str]:
    return event_tickets.ticket_line(row.get("price"), row.get("rsvp_url"))


def _event_payload(row) -> dict:
    """Times go out as UTC only — a naive local `date`/`end_at` is never sent.
    Categories go as `category_slugs` — create has no singular field."""
    payload = {
        "title": row["title"] or row["name"],
        "start_at": row["date_utc"],
    }
    description = event_tickets.with_ticket_line(row["description"], _ticket_line(row))
    if description:
        payload["description"] = description
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
    if row.get("accessibility_notes"):
        payload["accessibility_notes"] = row["accessibility_notes"]
    return payload




def _town_tz():
    from dateutil import tz as _tz
    return _tz.gettz(town_tz_name())


def _linked_names(conn, event_uid: str, kinds=("performer", "creator")) -> list[str]:
    marks = ",".join("?" for _ in kinds)
    names = []
    for lk in conn.execute(f"SELECT entity_kind, entity_uid FROM linked_events "
                           f"WHERE event_uid = ? AND entity_kind IN ({marks})",
                           (event_uid, *kinds)).fetchall():
        table = {"performer": "performers", "creator": "creators"}[lk["entity_kind"]]
        r = conn.execute(f"SELECT name FROM {table} WHERE entity_uid = ?",
                         (lk["entity_uid"],)).fetchone()
        if r and r["name"]:
            names.append(r["name"])
    return names


def _local_cluster_venues(conn, event: dict, key: str) -> set[str]:
    """Venue tokens of other local events whose title has this key."""
    import json
    from engine import series
    out: set[str] = set()
    for r in conn.execute(
            "SELECT e.title, e.venue_name, e.venue_raw, v.extra_json AS v_extra "
            "FROM events e LEFT JOIN venues v ON v.entity_uid = e.derived_venue_uid "
            "WHERE e.event_uid != ? AND COALESCE(e.status, '') NOT IN ('duplicate', 'superseded') "
            "  AND lower(substr(e.title, 1, 4)) = lower(substr(?, 1, 4))",
            (event["event_uid"], event.get("title") or "")).fetchall():
        if series.series_key(r["title"]) != key:
            continue
        try:
            live_id = (json.loads(r["v_extra"] or "{}") or {}).get("live_id")
        except (json.JSONDecodeError, AttributeError):
            live_id = None
        token = series.venue_token(live_id, r["venue_name"] or r["venue_raw"])
        if token:
            out.add(token)
    return out


# Live occurrences read either side of an event's start for source-branch
# evidence and performer Clusters.
_NEARBY_DAYS = 45
# At most this many candidate Series' occurrences are read per event.
_MAX_SERIES_READS = 5


def _title_decision(client, conn, event: dict, payload: dict):
    """The title to create the event under (`series.decide_title`), from the
    Series snapshot, live occurrences near it, its derived performers and
    other local events with its title. API errors raise — the caller treats
    them like an unanswered lookup."""
    from datetime import timedelta
    from engine import series
    from engine.derive_helpers import parse_instant
    from engine.live_lookup import events_between
    title = payload["title"]
    key = series.series_key(title)
    performers = _linked_names(conn, event["event_uid"])
    snapshot = series.load_snapshot(client)
    performer_only = any(series.series_key(p) == key for p in performers)
    nearby: list[dict] = []
    start = parse_instant(payload["start_at"])
    if start and ((snapshot and payload.get("source_url")) or performer_only):
        nearby = list(events_between(client, start - timedelta(days=_NEARBY_DAYS),
                                     start + timedelta(days=_NEARBY_DAYS)))
    seed_url = _seed_url(conn, event)
    slugs = series.candidate_slugs(title, payload.get("source_url"), snapshot, nearby,
                                   seed_url)[:_MAX_SERIES_READS]
    occurrences = {s: list(client.paginate("events", series=s)) for s in slugs}
    cluster: set[str] = set()
    if performer_only:
        cluster = _local_cluster_venues(conn, event, key) | {
            t for r in nearby if series.series_key(r.get("title")) == key
            for t in [series.venue_token(r.get("venue_id"), None)] if t}
    venue_name = _derived_venue_name(conn, event) or payload.get("venue_name")
    return series.decide_title(
        title, venue_id=payload.get("venue_id"), venue_name=venue_name,
        source_url=payload.get("source_url"), performers=performers, series=snapshot,
        occurrences=occurrences, cluster_venues=cluster, seed_url=seed_url)


def _derived_venue_name(conn, event: dict) -> Optional[str]:
    vuid = event.get("derived_venue_uid")
    r = conn.execute("SELECT name FROM venues WHERE entity_uid = ?", (vuid,)).fetchone() \
        if vuid else None
    return r["name"] if r and r["name"] else None


def _seed_url(conn, event: dict) -> Optional[str]:
    seed = (event.get("source") or "").removeprefix("seed:")
    r = conn.execute("SELECT url FROM event_seeds WHERE name = ?", (seed,)).fetchone()
    return r["url"] if r else None


def _ship_event(client, conn, event: dict, venue_slug: Optional[str]) -> Optional[str]:
    """Create the event live — unless it is already there (by slug, as an
    occurrence of its Series on the same town-local date, or by source_url
    at the same time), in which case the live row is filled in instead.
    Several live rows for it park the event for the operator."""
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
        source_title = payload["title"]
        decision = _title_decision(client, conn, event, payload)
        payload["title"] = decision.title
        record_activity(conn, _WORKER, "series-title", event_uid,
                        detail=decision.summary(source_title))
        found = find_live_event(client, slug=proposed_slug, source_url=payload.get("source_url"),
                                start_at=payload["start_at"], series_slug=decision.series_slug,
                                tz=_town_tz())
    except Exception as e:  # noqa: BLE001 — rc=1 → workqueue retry; never create blind
        msg = f"look up live event failed: {str(e)[:200]}"
        record_activity(conn, _WORKER, "find-live-event", event_uid, ok=False, error=msg)
        print(f"event {event_uid[:12]}: {msg}", file=sys.stderr)
        return None
    if found.ambiguous:
        slugs = ", ".join(sorted(r.get("slug") or "?" for r in found.ambiguous))
        why = (f"Series {decision.series_slug} already has several occurrences on this date"
               if found.matched_by == "series-day" else
               f"several live events share source_url {payload.get('source_url')} at this time")
        _park_for_review(conn, event_uid, "find-live-event",
                         f"{why}: {slugs} — duplicate for the operator")
        return None
    if found.row and isinstance(client, autonomy.GatedClient) \
            and found.row.get("status") == "rejected":
        return _decline_rejected(conn, event_uid, found)
    if found.row:
        if isinstance(client, autonomy.GatedClient) and found.row.get("status") == "pending":
            # Adopting a pending record (ours from an earlier run, or another
            # submitter's): follow it like our own submission.
            autonomy.record_submission(conn, "events", found.row["slug"],
                                       **{**client.context, "local_uid": event_uid,
                                          "series_slug": decision.series_slug})
        return _fill_live_event(client, conn, event, found.row, payload)

    idem = f"hermes-{os.environ.get('TOWNLOOPS_TOWN_SLUG','?')}:ship:event:{event_uid[:16]}"
    if isinstance(client, autonomy.GatedClient):
        client.context.update(kind="new", local_uid=event_uid,
                              series_slug=decision.series_slug)
    try:
        result = client.create("events", payload, idempotency_key=idem)
    except Exception as e:
        if isinstance(e, autonomy.SubmissionsHeld):
            raise
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
    if isinstance(result, dict) and not _repair_dropped_fields(client, conn, event, slug, payload, result):
        return None
    return slug


def _decline_rejected(conn, event_uid: str, found) -> object:
    """The moderator already rejected this event on TownLoops: never submit
    it again."""
    d = autonomy.Decision("drop", "rejected-on-townloops",
                          {"live_slug": found.row.get("slug"), "matched_by": found.matched_by})
    if event_state.decline(conn, event_uid, f"[autonomy] {d.reason}",
                           from_states=("entities-enriched",)):
        autonomy.record(conn, "event", event_uid, d)
    print(f"event {event_uid[:12]}: TownLoops rejected {found.row.get('slug')} — not resubmitted")
    return _DECLINED


def _submit_change(client, conn, event: dict, meta: dict,
                   venue_slug: Optional[str]) -> Optional[str]:
    """Autonomous: every source-side change event-reconcile staged for a live
    event lands somewhere. A change to a value the live row already has
    (date, venue, any other field) goes to the moderator as a pending event
    with the same source_url, for the TownLoops merge flow — the published
    event is never PATCHed with it. A change that only fills a blank live
    field, and the accessibility-notes / ticket-line / `free` fills, are
    PATCHed live and GET-verified like any fill-blank. Each outcome is a
    recorded `change` decision. Returns the pending event's slug, else the
    live slug."""
    from engine.live_patch import apply_event_patch, shows
    changes = meta.get("changes") or {}
    live_slug = meta["live_slug"]
    uid = event["event_uid"]
    try:
        current = client.get("events", live_slug) or {}
    except Exception as e:  # noqa: BLE001 — surfaced via rc=1 → workqueue retry
        print(f"read live event {live_slug} failed: {str(e)[:200]}", file=sys.stderr)
        return None
    payload = _event_payload(event)
    venue_id = _resolve_venue_id(client, conn, event, venue_slug)
    if venue_id:
        payload["venue_id"] = venue_id
    submit: dict[str, Any] = {}
    fill = _reconcile_fill_overrides(event, current)
    submitted_keys, skipped = [], {}
    for key in sorted(changes):
        values = {f: v for f, v in _change_values(key, event, payload, current).items()
                  if v is not None}
        differ = {f: v for f, v in values.items() if not shows(current, f, v)}
        if not differ:
            skipped[key] = "already-live" if values else "no-source-value"
        elif "venue_name" not in differ and all(_blank_on(current, f) for f in differ):
            fill.update(differ)
        else:
            submit.update(differ)
            submitted_keys.append(key)
    for key, why in skipped.items():
        autonomy.record(conn, "change", uid, autonomy.Decision(
            "skip", why, {"change": key, "target_slug": live_slug}))
    if fill:
        try:
            result = apply_event_patch(client, live_slug, fill, current=current)
        except Exception as e:  # noqa: BLE001 — surfaced via rc=1 → workqueue retry
            print(f"patch event {live_slug} failed: {str(e)[:200]}", file=sys.stderr)
            return None
        if result.mismatches:
            _flag_for_review(conn, uid, live_slug, result.mismatches)
            return None
        autonomy.record(conn, "change", uid, autonomy.Decision(
            "patch", "fill-blank-live", {"fields": sorted(fill), "target_slug": live_slug}))
    if not submit:
        return live_slug
    payload.update(submit)
    if "start_at" in submit and not submit.get("end_at"):
        payload.pop("end_at", None)   # never an end that may fall before the new start
    payload["slug"] = _slug_for("event", uid)
    client.context.update(kind="change", target_slug=live_slug, local_uid=uid)
    idem = f"hermes-{os.environ.get('TOWNLOOPS_TOWN_SLUG','?')}:change:event:{uid[:16]}"
    try:
        result = client.create("events", payload, idempotency_key=idem)
    except autonomy.SubmissionsHeld:
        raise
    except Exception as e:  # noqa: BLE001 — rc=1 → workqueue retry
        from engine.live_lookup import is_conflict
        if not is_conflict(e):
            record_activity(conn, _WORKER, "submit-change", uid, ok=False, error=str(e)[:1000])
            print(f"submit change for {live_slug} failed: {str(e)[:200]}", file=sys.stderr)
            return None
        result = {"slug": payload["slug"]}
    slug = (result or {}).get("slug") or payload["slug"]
    _remember_change_submission(conn, uid, slug, live_slug, submit)
    autonomy.record(conn, "change", uid, autonomy.Decision(
        "submit", "published-value-changed",
        {"changes": submitted_keys, "fields": sorted(submit), "target_slug": live_slug,
         "pending_slug": slug}))
    return slug


def _change_values(key: str, event: dict, payload: dict, current: dict) -> dict[str, Any]:
    """The event fields a reconcile change sets, with the values to send:
    a date change moves the end with it, a venue goes by live id (by name
    only when the venue isn't live — the moderator's curate-venue flow),
    any other change key is the payload field of that name."""
    if key == "start_at":
        return {"start_at": event["date_utc"],
                "end_at": _patched_end(event.get("end_at_utc"), current.get("start_at"),
                                       current.get("end_at"), event["date_utc"])}
    if key == "venue":
        if payload.get("venue_id"):
            return {"venue_id": payload["venue_id"]}
        return {"venue_name": payload["venue_name"]} if payload.get("venue_name") else {}
    return {key: payload[key]} if payload.get(key) not in (None, "", []) else {}


def _blank_on(live: dict, field: str) -> bool:
    if field in ("venue_id", "venue_name"):
        return not (live.get("venue_id") or live.get("venue_slug") or live.get("venue_name"))
    return live.get(field) in (None, "", [])


def _remember_change_submission(conn, event_uid: str, pending_slug: str, target_slug: str,
                                fields: dict) -> None:
    """What a change submission carried that the published event didn't show,
    so the moderation poll can tell a merge (the target now shows any of it)
    from a delete."""
    import json
    row = conn.execute("SELECT extra_json FROM events WHERE event_uid = ?",
                       (event_uid,)).fetchone()
    try:
        extra = json.loads((row["extra_json"] if row else None) or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        extra = {}
    extra["change_submission"] = {"pending_slug": pending_slug, "target_slug": target_slug,
                                  "fields": fields}
    conn.execute("UPDATE events SET extra_json = ? WHERE event_uid = ?",
                 (json.dumps(extra, ensure_ascii=False), event_uid))


def _fill_live_event(client, conn, event: dict, live: dict, payload: dict) -> Optional[str]:
    """Bring an event that is already live up to what we'd have created,
    without overriding anything on it: blank fields are filled, local
    categories/tags are added to the live sets, and a venue is bound only
    when it has none. Sent as a full-body PATCH and GET-verified; a mismatch
    parks the event for the operator. Returns the live slug."""
    from engine.live_patch import apply_event_patch
    slug = live["slug"]
    line = _ticket_line(event)
    if ((payload.get("accessibility_notes") and "accessibility_notes" not in live)
            or (line and "description" not in live)):
        # A source_url match is a list row; decide "blank" on the full row.
        try:
            live = client.get("events", slug) or live
        except Exception as e:  # noqa: BLE001 — surfaced via rc=1 → workqueue retry
            print(f"read live event {slug} failed: {str(e)[:200]}", file=sys.stderr)
            return None
    overrides = _fill_overrides(live, payload, event)
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


def _fill_overrides(live: dict, payload: dict, event: Optional[dict] = None) -> dict:
    event = event or {}
    out: dict[str, Any] = {}
    for k in ("title", "description", "start_at", "source_url", "accessibility_notes"):
        if payload.get(k) and not live.get(k):
            out[k] = payload[k]
    if live.get("description"):
        # The ticket line goes on top of the live words, replacing an earlier one.
        described = event_tickets.with_ticket_line(live["description"], _ticket_line(event))
        if described != live["description"]:
            out["description"] = described
    # A source_url match may start at another time; our end only fits ours.
    if (payload.get("end_at") and not live.get("end_at")
            and _to_dt(payload["start_at"]) == _to_dt(live.get("start_at"))):
        out["end_at"] = payload["end_at"]
    for k, cap in (("category_slugs", cats.MAX_CATEGORIES), ("tags", cats.MAX_TAGS)):
        have = list(live.get(k) or [])
        merged = (have + [v for v in payload.get(k) or [] if v not in have])[:max(cap, len(have))]
        if k == "tags":
            merged = event_tickets.live_tags(merged, event.get("price"))
        if merged != have:
            out[k] = merged
    if payload.get("venue_id") and not live.get("venue_id"):
        out["venue_id"] = payload["venue_id"]
    return out


def _repair_dropped_fields(client, conn, event: dict, slug: str, payload: dict,
                           created: dict) -> bool:
    """A create path has been seen to drop `tags` (CMO, bulk create). When the
    created row lacks a category/tag set or the accessibility notes that were
    sent, PATCH them in as a full body and GET-verify. False when it still
    doesn't stick — the event is then flagged for the operator, its live slug
    kept so nothing re-creates it."""
    from engine.live_patch import apply_event_patch
    dropped = {k: payload[k] for k in ("category_slugs", "tags")
               if payload.get(k) and sorted(payload[k]) != sorted(created.get(k) or [])}
    if payload.get("accessibility_notes") and not created.get("accessibility_notes"):
        dropped["accessibility_notes"] = payload["accessibility_notes"]
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
    Accessibility notes fill the live row only when it has none; the ticket
    line tops the live description and a free event gains `free`. Returns the
    live slug, or None (→ rc 1) when the change can't be applied correctly or
    didn't land as sent (then the event is flagged stuck)."""
    from engine.live_patch import apply_event_patch
    changes = meta.get("changes") or {}
    live_slug = meta["live_slug"]
    if not ({"start_at", "venue"} & changes.keys()) and not (
            event.get("accessibility_notes") or _ticket_line(event) or event.get("price")):
        return live_slug
    try:
        current = client.get("events", live_slug) or {}
    except Exception as e:  # noqa: BLE001 — surfaced via rc=1 → workqueue retry
        print(f"read live event {live_slug} failed: {str(e)[:200]}", file=sys.stderr)
        return None
    overrides = _reconcile_fill_overrides(event, current)
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
    if not overrides:
        return live_slug
    try:
        result = apply_event_patch(client, live_slug, overrides, current=current)
    except Exception as e:  # noqa: BLE001 — surfaced via rc=1 → workqueue retry
        print(f"patch event {live_slug} failed: {str(e)[:200]}", file=sys.stderr)
        return None
    if result.mismatches:
        _flag_for_review(conn, event["event_uid"], live_slug, result.mismatches)
        return None
    return live_slug


def _reconcile_fill_overrides(event: dict, current: dict) -> dict[str, Any]:
    """The fills a reconcile-staged change brings to the live row: accessibility
    notes only when it has none, the ticket line on top of its description,
    `free` added to (or dropped from) its tags by the stated price."""
    overrides: dict[str, Any] = {}
    notes = event.get("accessibility_notes")
    line = _ticket_line(event)
    if notes and not current.get("accessibility_notes"):
        overrides["accessibility_notes"] = notes
    described = event_tickets.with_ticket_line(
        current.get("description") or event.get("description"), line)
    if line and described != current.get("description"):
        overrides["description"] = described
    tags = event_tickets.live_tags(list(current.get("tags") or []), event.get("price"))
    if tags != list(current.get("tags") or []):
        overrides["tags"] = tags
    return overrides


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


def _after_ship_series(client, conn, event_uid: str, slug: str) -> None:
    """Re-read the shipped event: mirror the Series TownLoops put it in, and
    fill its blank Series-shared fields from its siblings. Best-effort — the
    event is live either way, and entity-live-sync's daily sweep retries."""
    from engine import series_sync
    try:
        row = client.get("events", slug) or {}
    except Exception as e:  # noqa: BLE001
        print(f"read live event {slug} failed: {str(e)[:200]}", file=sys.stderr)
        return
    if not row.get("slug"):
        return
    series_sync.mirror(conn, [row])   # keyed by townloops_slug, set by _mark_shipped
    if row.get("series_slug"):
        series_sync.fill_and_record(client, conn, row, _WORKER, event_uid)


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

    mode = autonomy.settings()
    if mode.autonomous:
        filled = event_dedup.best_of_copies(conn, event)
        if filled:
            record_activity(conn, _WORKER, "best-of-copies", event_uid,
                            detail=", ".join(f"{k} from {v[:12]}" for k, v in filled.items()))
            event = load_event(conn, event_uid)

    if not _ensure_utc_times(conn, event):
        # rc 1 → workqueue retries, then marks the event stuck for the operator.
        msg = f"no UTC start time (date={event.get('date')!r}) — not shipped"
        record_activity(conn, _WORKER, "ship-event", event_uid, ok=False, error=msg)
        print(f"event {event_uid[:12]}: {msg}", file=sys.stderr)
        return 1

    sibling = None if _reconcile_change(event) else event_dedup.shipped_sibling(conn, event)
    if sibling:
        # Another copy of this event (another seed) is already live: never
        # create a second one. Retire this copy onto the live event.
        conn.execute("UPDATE events SET status = 'duplicate' WHERE event_uid = ?", (event_uid,))
        _mark_shipped(conn, event_uid, sibling["townloops_slug"])
        # Same live row, so the Series TownLoops last reported for it.
        conn.execute("UPDATE events SET series_slug = ?, series_id = ? WHERE event_uid = ?",
                     (sibling.get("series_slug"), sibling.get("series_id"), event_uid))
        record_activity(conn, _WORKER, "duplicate-of", event_uid,
                        detail=f"{sibling['event_uid'][:12]} already live as {sibling['townloops_slug']}")
        print(f"event {event_uid[:12]}: duplicate of {sibling['event_uid'][:12]} — not shipped")
        return 0

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

    if mode.autonomous:
        gate = autonomy.submission_gate(conn, mode)
        if gate.verdict != "submit":
            print(f"event {event_uid[:12]}: submissions held ({gate.reason}) — not now")
            return RC_DEFERRED
        client = autonomy.GatedClient(client, conn, mode)
        seed = (event.get("source") or "").removeprefix("seed:") or None
        client.context = {"seed": seed, "discovery_source": f"event:{seed}" if seed else None}
    try:
        return _ship(client, conn, event)
    except autonomy.SubmissionsHeld as e:
        print(f"event {event_uid[:12]}: submissions held ({e.reason}) — not now")
        return RC_DEFERRED


def _ship(client, conn, event: dict) -> int:
    event_uid = event["event_uid"]

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
    if change and isinstance(client, autonomy.GatedClient):
        event_slug = _submit_change(client, conn, event, change, venue_slug)
    elif change:
        event_slug = _patch_live_event(client, conn, event, change, venue_slug=venue_slug)
        if event_slug:
            _supersede_older_copies(conn, event_uid, event_slug)
    else:
        event_slug = _ship_event(client, conn, event, venue_slug)
    if event_slug is _DECLINED:
        return 0
    if not event_slug:
        return 1

    _mark_shipped(conn, event_uid, event_slug, disabled=False)
    _after_ship_series(client, conn, event_uid, event_slug)
    event_detail.drop_cached_page(event_uid)
    print(f"shipped event {event_uid[:12]} → {event_slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
