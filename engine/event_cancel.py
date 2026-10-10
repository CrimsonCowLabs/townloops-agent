"""Cancellation detection: events that vanish from their sources before they
happen.

    (live) ──N misses──▶ possibly-cancelled ──confirm──▶ confirmed ──apply──▶ cancelled
                              │    ▲                                  └──▶ apply-failed
                              │    └─ seen again: flag cleared              (re-confirm, or
                              │                                              cleared if seen)
                              └──dismiss──▶ dismissed (never flagged again)

Bookkeeping, after each successful event-find run of a seed
(`record_seed_run`): every upcoming row from that seed the run should have
listed — dated no later than the furthest event it did list, since many
calendars only show the next few weeks — is either seen (`missed_finds`
reset) or missed (`missed_finds` + 1). An empty or failed run counts nothing.

`flag_vanished` then looks at each upcoming event that is on TownLoops: when
every seed-found copy of it (all rows sharing its `canonical_event_uid`,
engine/event_dedup) has missed N finds in a row, its shipped row is flagged
`possibly-cancelled`. Nothing is changed live by detection — only the
operator's confirmation (`confirm`, from the dashboard Confirm widget) lets
`apply_confirmed` mark it cancelled on TownLoops.

TownLoops has no cancelled status (EventPatch can't set `status`; DELETE
removes the page), so a cancelled event is marked in place: the title gets a
"Cancelled: " prefix and the `cancelled` tag is added, as a full-body PATCH
(engine/live_patch) with GET verify. A PATCH that doesn't land parks the
event as `apply-failed` for the operator instead of being retried.

Autonomous mode (engine/autonomy) has no confirm step and never PATCHes a
published title: `submit_evidenced` re-reads a flagged event's own page, and
only when the page itself says it is cancelled or postponed submits a
pending "Cancelled: " copy carrying its source_url, for the TownLoops merge
flow (`cancel_status` 'submitted'). A flag with no such statement stays
flagged and is listed in the nightly digest — never acted on.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional

from engine.derive_helpers import dict_rows as _rows, iso_utc_now


DEFAULT_MISS_THRESHOLD = 4   # event-find runs twice a day → two days unlisted
CANCELLED_PREFIX = "Cancelled: "
CANCELLED_TAG = "cancelled"
_TITLE_MAX = 200
_TAGS_MAX = 10   # EventPatch.tags max_length

ProposalKind = Literal["confirm", "dismiss"]
_PROPOSAL_KINDS = {"confirm": "confirm_cancellations", "dismiss": "dismiss_cancellations"}
# The operator has decided about the event: detection leaves it alone.
_DECIDED = ("confirmed", "cancelled", "dismissed")
# 'submitted': autonomous mode sent a pending cancellation to TownLoops.
_DECIDED = (*_DECIDED, "submitted")
# Waiting on the operator: what confirm/dismiss act on.
_FLAGGED = ("possibly-cancelled", "apply-failed")


def record_seed_run(conn: sqlite3.Connection, seed_name: str, seen_uids: Iterable[str],
                    listed_dates: Iterable[Optional[str]],
                    now: Optional[datetime] = None) -> dict:
    """Count a successful find of `seed_name` — which listed the rows
    `seen_uids`, dated `listed_dates` — against the seed's upcoming rows:
    the seen ones reset, the rest gain a miss."""
    days = [d[:10] for d in listed_dates if isinstance(d, str) and len(d) >= 10]
    if not days:
        return {"seen": 0, "missed": 0}
    seen_set = set(seen_uids)
    rows = conn.execute(
        "SELECT event_uid FROM events WHERE source = ? AND status IN ('active', 'duplicate') "
        "AND date_utc > ? AND date_day <= ?",
        (f"seed:{seed_name}", _iso(now), max(days))).fetchall()
    out = {"seen": 0, "missed": 0}
    for (uid,) in rows:
        if uid in seen_set:
            conn.execute("UPDATE events SET missed_finds = 0 WHERE event_uid = ?", (uid,))
            out["seen"] += 1
        else:
            conn.execute("UPDATE events SET missed_finds = missed_finds + 1 WHERE event_uid = ?",
                         (uid,))
            out["missed"] += 1
    return out


def flag_vanished(conn: sqlite3.Connection, threshold: int = DEFAULT_MISS_THRESHOLD,
                  now: Optional[datetime] = None) -> dict:
    """Flag upcoming live events missing from all their sources for
    `threshold` finds; clear the flag on any that were seen again."""
    flagged, cleared = [], []
    groups = [r[0] for r in conn.execute(
        "SELECT DISTINCT COALESCE(canonical_event_uid, event_uid) FROM events "
        "WHERE townloops_slug IS NOT NULL AND status != 'superseded' AND date_utc > ? "
        "ORDER BY 1", (_iso(now),)).fetchall()]
    for g in groups:
        members = _rows(conn, "SELECT * FROM events WHERE (canonical_event_uid = ? OR event_uid = ?) "
                              "AND status != 'superseded'", (g, g))
        if any(m["cancel_status"] in _DECIDED for m in members):
            continue
        sources = [m for m in members if (m["source"] or "").startswith("seed:")]
        # One row per live event: a copy event-ship retired onto a sibling's
        # slug (status 'duplicate') shares that slug and isn't flagged again.
        shipped = [m for m in members if m["townloops_slug"] and m["status"] != "duplicate"]
        if sources and all(m["missed_finds"] >= threshold for m in sources):
            names = ", ".join(sorted({m["source"][len("seed:"):] for m in sources}))
            note = f"not listed by {names} for {threshold}+ finds in a row"
            for m in shipped:
                if m["cancel_status"] is None:
                    conn.execute(
                        "UPDATE events SET cancel_status = 'possibly-cancelled', "
                        "  cancel_flagged_at = ?, cancel_note = ? WHERE event_uid = ?",
                        (iso_utc_now(), note, m["event_uid"]))
                    flagged.append(m["event_uid"])
        else:
            for m in shipped:
                if m["cancel_status"] in _FLAGGED:
                    conn.execute(
                        "UPDATE events SET cancel_status = NULL, cancel_flagged_at = NULL, "
                        "  cancel_note = NULL WHERE event_uid = ?", (m["event_uid"],))
                    cleared.append(m["event_uid"])
    return {"flagged": flagged, "cleared": cleared}


# --------------------------------------------------------------------------
# Operator decisions
# --------------------------------------------------------------------------

def flagged(conn: sqlite3.Connection) -> list[dict]:
    """Events waiting on the operator, soonest first."""
    return _rows(conn, "SELECT * FROM events WHERE cancel_status IN (?, ?) "
                       "ORDER BY date_utc, event_uid", _FLAGGED)


def confirm(conn: sqlite3.Connection, uids: Iterable[str]) -> dict:
    """The operator says these are cancelled: the next `apply_confirmed`
    marks them cancelled on TownLoops."""
    return _flip(conn, uids, "confirmed")


def dismiss(conn: sqlite3.Connection, uids: Iterable[str]) -> dict:
    """The operator says these are still on: never flagged again."""
    uids = list(uids)
    out = _flip(conn, uids, "dismissed")
    for uid in set(uids) - set(out["skipped_uids"]):
        conn.execute("UPDATE events SET missed_finds = 0 WHERE event_uid = ? OR "
                     "canonical_event_uid = (SELECT canonical_event_uid FROM events "
                     "WHERE event_uid = ?)", (uid, uid))
    return out


def propose(conn: sqlite3.Connection, kind: ProposalKind,
            uids: Optional[list[str]] = None, limit: int = 50) -> dict:
    """A read-only proposal for the chat Confirm widget: the flagged events
    — all, or those whose uid starts with one of `uids`. `proposal` is None
    when nothing matches."""
    rows = flagged(conn)
    if uids:
        rows = [r for r in rows if any(r["event_uid"].startswith(u) for u in uids if u)]
    rows = rows[:limit]
    if not rows:
        return {"proposal": None, "preview": []}
    return {"proposal": {"kind": _PROPOSAL_KINDS[kind],
                         "uids": [r["event_uid"] for r in rows], "count": len(rows)},
            "preview": [preview(r) for r in rows]}


def preview(row: dict) -> dict:
    return {"event_uid": row["event_uid"], "title": row["title"] or row["name"],
            "date": row["date_local"] or row["date"], "live_slug": row["townloops_slug"],
            "status": row["cancel_status"], "note": row["cancel_note"]}


def _flip(conn: sqlite3.Connection, uids: Iterable[str], status: str) -> dict:
    done, skipped = 0, []
    for uid in uids:
        cur = conn.execute(
            "UPDATE events SET cancel_status = ? WHERE event_uid = ? "
            "AND cancel_status IN (?, ?)", (status, uid, *_FLAGGED))
        if cur.rowcount:
            done += 1
        else:
            skipped.append(uid)
    conn.commit()
    return {status: done, "skipped": len(skipped), "skipped_uids": skipped}


# --------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------

def cancelled_overrides(live: dict) -> dict:
    """What marks a live event cancelled: the title prefix and the tag. Empty
    when it already shows both."""
    out: dict = {}
    title = live.get("title") or ""
    if not title.lower().startswith(CANCELLED_PREFIX.lower()):
        out["title"] = (CANCELLED_PREFIX + title)[:_TITLE_MAX]
    tags = list(live.get("tags") or [])
    if CANCELLED_TAG not in tags:
        out["tags"] = tags[:_TAGS_MAX - 1] + [CANCELLED_TAG]
    return out


def apply_confirmed(conn: sqlite3.Connection, client, limit: int = 20) -> dict:
    """Mark operator-confirmed cancellations on TownLoops. An event already
    gone from TownLoops counts as cancelled."""
    from engine.live_lookup import is_not_found
    from engine.live_patch import apply_event_patch
    out = {"cancelled": 0, "failed": 0, "errors": []}
    rows = _rows(conn, "SELECT * FROM events WHERE cancel_status = 'confirmed' "
                       "AND townloops_slug IS NOT NULL ORDER BY date_utc LIMIT ?", (limit,))
    for row in rows:
        uid, slug = row["event_uid"], row["townloops_slug"]
        try:
            live = client.get("events", slug) or {}
        except Exception as e:  # noqa: BLE001
            if is_not_found(e):
                _set(conn, uid, "cancelled", f"live event {slug} not on TownLoops any more")
                out["cancelled"] += 1
            else:  # transient: stays confirmed for the next run
                out["errors"].append(f"{slug}: {str(e)[:200]}")
            continue
        overrides = cancelled_overrides(live)
        try:
            mismatches = (apply_event_patch(client, slug, overrides, current=live).mismatches
                          if overrides else [])
        except Exception as e:  # noqa: BLE001 — parked, not retried blind
            mismatches = [f"PATCH failed: {str(e)[:200]}"]
        if mismatches:
            _set(conn, uid, "apply-failed", f"marking {slug} cancelled didn't land: "
                 + "; ".join(mismatches))
            out["failed"] += 1
            out["errors"].append(f"{slug}: {'; '.join(mismatches)[:200]}")
        else:
            _set(conn, uid, "cancelled", f"marked cancelled on TownLoops ({slug})")
            out["cancelled"] += 1
    return out


RECHECK_HOURS = 24


def submit_evidenced(conn: sqlite3.Connection, client, fetch, limit: int = 3,
                     now: Optional[datetime] = None) -> dict:
    """Autonomous: re-read up to `limit` flagged events' own pages (each at
    most once per RECHECK_HOURS) and submit a cancellation for those whose
    page explicitly says so. `client` is an autonomy.GatedClient; `fetch(url)
    → (ok, markdown, err)`."""
    import json
    import os
    from datetime import timedelta
    from engine import autonomy
    now = now or datetime.now(timezone.utc)
    out = {"checked": 0, "submitted": 0, "no_statement": 0, "errors": []}
    cutoff = (now - timedelta(hours=RECHECK_HOURS)).isoformat(timespec="seconds")
    rows = [r for r in _rows(conn, "SELECT * FROM events WHERE cancel_status = 'possibly-cancelled' "
                                   "AND townloops_slug IS NOT NULL AND COALESCE(source_url, '') != '' "
                                   "ORDER BY date_utc")
            if (autonomy.extra(r).get("autonomy") or {}).get("cancel_checked_at", "") < cutoff]
    for row in rows[:limit]:
        uid, slug = row["event_uid"], row["townloops_slug"]
        out["checked"] += 1
        extra = autonomy.extra(row)
        extra["autonomy"] = {**(extra.get("autonomy") or {}), "cancel_checked_at": iso_utc_now()}
        conn.execute("UPDATE events SET extra_json = ? WHERE event_uid = ?", (json.dumps(extra), uid))
        conn.commit()
        ok, page, err = fetch(row["source_url"])
        d = autonomy.cancellation_evidence(page if ok else None, row["title"] or row["name"])
        if d.verdict != "submit":
            out["no_statement"] += 1
            autonomy.record(conn, "cancellation", uid, d)
            conn.commit()
            continue
        try:
            live = client.get("events", slug) or {}
            payload = {k: live[k] for k in ("title", "description", "start_at", "end_at",
                                            "venue_id", "source_url", "category_slugs", "tags",
                                            "accessibility_notes") if live.get(k) is not None}
            payload.update(cancelled_overrides(live))
            payload["slug"] = f"hermes-{os.environ.get('TOWNLOOPS_TOWN_SLUG', 'unknown')}-cancel-{uid[:12]}"
            client.context.update(kind="cancellation", target_slug=slug, local_uid=uid)
            created = client.create("events", payload,
                                    idempotency_key=f"hermes-{os.environ.get('TOWNLOOPS_TOWN_SLUG', '?')}:cancel:event:{uid[:16]}")
        except autonomy.SubmissionsHeld as e:
            out["errors"].append(f"{slug}: held ({e.reason})")
            break
        except Exception as e:  # noqa: BLE001 — stays flagged; re-checked tomorrow
            out["errors"].append(f"{slug}: {str(e)[:200]}")
            continue
        d.evidence.update(target_slug=slug, pending_slug=(created or {}).get("slug"))
        autonomy.record(conn, "cancellation", uid, d)
        _set(conn, uid, "submitted", f"cancellation submitted for merge into {slug}: "
             f"{d.evidence['quote'][:200]}")
        out["submitted"] += 1
    return out


def _set(conn: sqlite3.Connection, uid: str, status: str, note: str) -> None:
    conn.execute("UPDATE events SET cancel_status = ?, cancel_note = ? WHERE event_uid = ?",
                 (status, note[:1000], uid))
    conn.commit()


def _iso(now: Optional[datetime]) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")

