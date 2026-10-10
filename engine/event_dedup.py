"""Cross-source event dedup: one canonical event per real-world event.

`event_uid` is keyed by seed, so the same concert listed by the venue, the
chamber and a newspaper calendar is three rows. Each new row is linked to a
`canonical_event_uid` shared with every other copy of the event — same local
day, matching title (`derive_helpers.names_match`), and no contradiction in
venue or start time. A copy with no venue or no time matches either way;
two different venues, or start times an hour or more apart (matinee and
evening shows), are different events.

Only one copy per canonical event goes forward; the others become
`status='duplicate'`, declined with a note naming the copy that stands, so
they never reach the operator's queue or ship:

- a copy the operator already decided (approved, declined, in the pipeline
  or shipped) or a live mirror always stands — nothing displaces it;
- otherwise the pending copy from the best source stands: a primary source
  beats a syndicator (`seed_role`), then the earliest found.

Ported from the legacy event-ingest syndicator suppression (canonical
sources win over `source_role: syndicator`), without its parsers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from engine.derive_helpers import dict_rows as _rows, iso_utc_now, names_match, normalize_name


PRIMARY, SYNDICATOR = "primary", "syndicator"

# Seed kinds that republish other organisations' events.
_SYNDICATOR_KINDS = {"newspaper", "tourism"}
# Aggregator hosts: listings scraped or submitted from elsewhere.
SYNDICATOR_HOSTS = {
    "allevents.in", "eventbrite.com", "evvnt.com", "patch.com", "bandsintown.com",
    "songkick.com", "eventful.com", "10times.com", "everfest.com", "do512.com",
}

# Start times this far apart are different occurrences (matinee vs evening).
_DISTINCT_TIMES_MIN = 60


def seed_role(seed: dict) -> str:
    """'primary' or 'syndicator'. The operator's `source_role` wins; else
    known aggregator hosts and newspaper/tourism calendars are syndicators."""
    if seed.get("source_role") in (PRIMARY, SYNDICATOR):
        return seed["source_role"]
    host = (urlparse(seed.get("url") or "").hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if any(host == h or host.endswith("." + h) for h in SYNDICATOR_HOSTS):
        return SYNDICATOR
    return SYNDICATOR if seed.get("kind") in _SYNDICATOR_KINDS else PRIMARY


def fingerprint(title: Optional[str], day: Optional[str], venue: Optional[str]) -> str:
    key = f"{normalize_name(title or '')}::{day or ''}::{normalize_name(venue or '')}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def link(conn: sqlite3.Connection, event_uid: str) -> Optional[str]:
    """Give the event its canonical uid (joining an existing copy's, or a new
    fingerprint) and settle which copy of that event stands. Idempotent.
    Returns the canonical uid, or None for an unknown/retired row."""
    ev = _get(conn, event_uid)
    if not ev or ev["status"] == "superseded":
        return None
    canonical = ev["canonical_event_uid"]
    if not canonical:
        match = next((r for r in _same_day(conn, ev) if same_event(ev, r)), None)
        if match is None:
            canonical = _unused_fingerprint(conn, ev)
        else:
            canonical = match["canonical_event_uid"] or _unused_fingerprint(conn, match)
            conn.execute("UPDATE events SET canonical_event_uid = ? WHERE event_uid = ?",
                         (canonical, match["event_uid"]))
        conn.execute("UPDATE events SET canonical_event_uid = ? WHERE event_uid = ?",
                     (canonical, event_uid))
    _settle(conn, canonical)
    return canonical


def backfill(conn: sqlite3.Connection) -> int:
    """Link every active row that has no canonical uid yet (rows from before
    dedup), oldest first. Returns how many there were."""
    uids = [r[0] for r in conn.execute(
        "SELECT event_uid FROM events WHERE canonical_event_uid IS NULL AND status = 'active' "
        "ORDER BY first_seen_at, event_uid").fetchall()]
    for uid in uids:
        link(conn, uid)
    return len(uids)


def shipped_sibling(conn: sqlite3.Connection, event: dict) -> Optional[dict]:
    """Another copy of this event that is already on TownLoops, if any. A
    copy retired onto a sibling's slug (status 'duplicate') doesn't count."""
    if not event.get("canonical_event_uid"):
        return None
    rows = _rows(conn,
                 "SELECT * FROM events WHERE canonical_event_uid = ? AND event_uid != ? "
                 "  AND townloops_slug IS NOT NULL AND status = 'active' "
                 "ORDER BY pushed_at IS NULL, first_seen_at LIMIT 1",
                 (event["canonical_event_uid"], event["event_uid"]))
    return rows[0] if rows else None


# Fields one copy of an event can lend another (groups fill together).
_LENDABLE = (("description",), ("end_at", "end_at_local", "end_at_utc"),
             ("venue_name", "venue_raw"), ("location", "location_raw"), ("source_url",),
             ("accessibility_notes",), ("price",), ("rsvp_url",), ("image_url",))


def best_of_copies(conn: sqlite3.Connection, event: dict) -> dict[str, str]:
    """Fill the event's blank fields from the other copies of it (same
    `canonical_event_uid`): each from the most trusted copy that has it —
    a primary source before a syndicator, then the seed's trust score, then
    the earliest found. Only NULL/empty fields are written. Returns
    {field: donor event_uid}."""
    canonical = event.get("canonical_event_uid")
    if not canonical:
        return {}
    copies = _rows(conn, "SELECT e.*, s.trust_score AS _trust FROM events e "
                         "LEFT JOIN event_seeds s ON e.source = 'seed:' || s.name "
                         "WHERE e.canonical_event_uid = ? AND e.event_uid != ? "
                         "AND e.status IN ('active', 'duplicate')",
                   (canonical, event["event_uid"]))
    if not copies:
        return {}
    copies.sort(key=lambda c: (_role_of(conn, c) != PRIMARY, -(c.get("_trust") or 0),
                               c["first_seen_at"], c["event_uid"]))
    filled: dict[str, str] = {}
    sets: dict[str, object] = {}
    for group in _LENDABLE:
        lead = group[0]
        if event.get(lead) not in (None, ""):
            continue
        donor = next((c for c in copies if c.get(lead) not in (None, "")), None)
        if donor is None:
            continue
        for col in group:
            if event.get(col) in (None, "") and donor.get(col) not in (None, ""):
                sets[col] = donor[col]
        filled[lead] = donor["event_uid"]
    if sets:
        conn.execute(f"UPDATE events SET {', '.join(f'{k} = ?' for k in sets)} "
                     "WHERE event_uid = ?", (*sets.values(), event["event_uid"]))
    return filled


def same_event(a: dict, b: dict) -> bool:
    """Same local day, matching titles, and no conflicting venue or time."""
    if not a.get("date_day") or a.get("date_day") != b.get("date_day"):
        return False
    if not names_match(_title(a), _title(b)):
        return False
    va, vb = _venue(a), _venue(b)
    if va and vb and not names_match(va, vb):
        return False
    ta, tb = _start(a), _start(b)
    if ta and tb and abs((ta - tb).total_seconds()) >= _DISTINCT_TIMES_MIN * 60:
        return False
    return True


# --------------------------------------------------------------------------

def _settle(conn: sqlite3.Connection, canonical: str) -> None:
    members = _rows(conn, "SELECT * FROM events WHERE canonical_event_uid = ? AND status = 'active' "
                          "ORDER BY first_seen_at, event_uid", (canonical,))
    decided = [m for m in members if _decided(m)]
    pending = [m for m in members if not _decided(m)]
    if decided:
        winner, losers = _best_decided(decided), pending
    elif pending:
        roles = {m["event_uid"]: _role_of(conn, m) for m in pending}
        winner = min(pending, key=lambda m: (roles[m["event_uid"]] != PRIMARY,
                                             m["first_seen_at"], m["event_uid"]))
        losers = [m for m in pending if m is not winner]
    else:
        return
    for m in losers:
        _mark_duplicate(conn, m, winner)


def _decided(m: dict) -> bool:
    return (m.get("approval_status") != "pending" or m.get("state") != "metadata-extracted"
            or bool(m.get("townloops_slug")) or m.get("source_kind") == "live")


def _best_decided(rows: list[dict]) -> dict:
    return min(rows, key=lambda m: (not m.get("townloops_slug"),
                                    m.get("approval_status") == "declined",
                                    m["first_seen_at"]))


def _mark_duplicate(conn: sqlite3.Connection, dup: dict, winner: dict) -> None:
    now = iso_utc_now()
    where = (winner.get("discovered_via") or winner.get("source") or "?").removeprefix("seed:")
    note = f"[dedup] same event as {winner['event_uid'][:12]} ({_title(winner)!r} from {where})"
    extra = _extra(dup)
    extra["duplicate_of"] = winner["event_uid"]
    conn.execute(
        "UPDATE events SET status = 'duplicate', approval_status = 'declined', "
        "  approval_note = ?, approval_at = ?, state = 'declined', state_updated_at = ?, "
        "  extra_json = ? "
        "WHERE event_uid = ? AND status = 'active' AND approval_status = 'pending' "
        "  AND state = 'metadata-extracted'",
        (note, now, now, json.dumps(extra), dup["event_uid"]),
    )


def _unused_fingerprint(conn: sqlite3.Connection, ev: dict) -> str:
    """A fingerprint no other group holds. Two events can share title, day
    and venue yet not be the same (matinee and evening show): the second
    one's fingerprint is salted with its own uid."""
    fp = fingerprint(_title(ev), ev["date_day"], _venue(ev))
    taken = conn.execute("SELECT 1 FROM events WHERE canonical_event_uid = ? LIMIT 1",
                         (fp,)).fetchone()
    return hashlib.sha1(f"{fp}::{ev['event_uid']}".encode()).hexdigest() if taken else fp


def _same_day(conn: sqlite3.Connection, ev: dict) -> list[dict]:
    """Other sources' rows on the event's day. A second row from the same
    seed on the same day is the source changing the event (a new time makes
    a new uid) — that goes to review, not into dedup."""
    return _rows(conn,
                 "SELECT * FROM events WHERE date_day = ? AND event_uid != ? AND source != ? "
                 "  AND status IN ('active', 'duplicate') "
                 "ORDER BY canonical_event_uid IS NULL, first_seen_at, event_uid",
                 (ev["date_day"], ev["event_uid"], ev["source"]))


def _role_of(conn: sqlite3.Connection, ev: dict) -> str:
    source = ev.get("source") or ""
    if not source.startswith("seed:"):
        return PRIMARY
    rows = _rows(conn, "SELECT * FROM event_seeds WHERE name = ? ORDER BY seed_id LIMIT 1",
                 (source[len("seed:"):],))
    return seed_role(rows[0]) if rows else PRIMARY


def _get(conn: sqlite3.Connection, uid: str) -> Optional[dict]:
    rows = _rows(conn, "SELECT * FROM events WHERE event_uid = ?", (uid,))
    return rows[0] if rows else None


def _title(ev: dict) -> str:
    return ev.get("title") or ev.get("name") or ""


def _venue(ev: dict) -> str:
    return ev.get("venue_name") or ev.get("venue_raw") or ""


def _start(ev: dict) -> Optional[datetime]:
    """The event's start instant, only when its source gave a time of day."""
    from engine.event_extract import to_utc  # event_extract imports this module
    raw = ev.get("date") or ""
    if len(raw) <= 10 or raw[11:16] in ("", "00:00"):
        return None
    value = ev.get("date_utc") or to_utc(raw)
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _extra(ev: dict) -> dict:
    try:
        extra = json.loads(ev.get("extra_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return extra if isinstance(extra, dict) else {}
