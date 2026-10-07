"""Shared helpers for the event-derive-* skills (M3).

Skill contract with orchestrator._invoke_skill:
- Invoked as: python <entry>.py --event-uid <uid>
- Return 0 on success (orchestrator advances state)
- Return non-zero on failure (orchestrator retries up to 3, then stuck)
- Skills should NOT advance events.state themselves — orchestrator owns
  the state machine.

The helpers here centralize:
- CLI arg parsing (every skill takes just --event-uid)
- DB open with WAL + row_factory
- Event loading
- Normalized-name matching (venue dedup, host dedup) and fuzzy entity-name
  matching against live TownLoops rows (shared with event-reconcile)
- entity_uid minting
- linked_events insert
- Activity recording
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional


_DB_PATH = Path(os.environ.get(
    "TOWNLOOPS_DB_PATH",
    os.path.join(
        os.environ.get("HERMES_HOME", "/home/agent/.hermes"),
        "state", "townloops.db",
    ),
))


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def town_name() -> str:
    """The town's display name from town-profile.yaml (search queries,
    locality checks), falling back to the slug."""
    import yaml
    path = Path(os.environ.get("TOWN_PROFILE_PATH") or os.path.join(
        os.environ.get("HERMES_HOME", "/home/agent/.hermes"), "town-profile.yaml"))
    try:
        profile = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        profile = {}
    return profile.get("name") or os.environ.get("TOWNLOOPS_TOWN_SLUG", "")


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def parse_event_uid_arg(argv: Optional[list[str]] = None) -> str:
    p = argparse.ArgumentParser()
    p.add_argument("--event-uid", required=True)
    args = p.parse_args(argv)
    return args.event_uid


def load_event(conn: sqlite3.Connection, event_uid: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM events WHERE event_uid = ?", (event_uid,),
    ).fetchone()
    return dict(row) if row else None


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def normalize_name(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Used for
    dedup lookups: `Kings Beach Community Ctr.` == `kings beach community ctr`."""
    s = (s or "").strip().lower()
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return s


NAME_MATCH_RATIO = 0.88


def names_match(a: Optional[str], b: Optional[str],
                ratio: float = NAME_MATCH_RATIO) -> bool:
    """Loose entity-name equality. Exact normalized match, high similarity,
    or token containment when the shorter name is specific enough
    ("Gunbarrel" vs "Gunbarrel Tavern" yes; "Tahoe" vs anything no)."""
    na, nb = normalize_name(a or ""), normalize_name(b or "")
    if not na or not nb:
        return False
    if na == nb:
        return True
    if SequenceMatcher(None, na, nb).ratio() >= ratio:
        return True
    short, long_ = sorted((na, nb), key=len)
    if len(short) >= 8 and set(short.split()) <= set(long_.split()):
        return True
    return False


# Port of the CMO's `_remote_entity_richness`: when TownLoops holds several
# same-name rows, bind to the most complete one, not merely the first listed.
_RICHNESS_WEIGHTS = {
    "description": 3, "address": 3, "category_slug": 2, "accessibility_notes": 2,
    "bio": 2, "meeting_info": 2, "image_path": 2,
}
_NOT_CONTENT = {"id", "slug", "name", "url", "town_id", "town_slug", "updated_at",
                "category_id", "entity_uid"}


def entity_richness(row: dict) -> tuple:
    """Sort key: weighted populated fields, then field count, then the most
    recently updated row, then slug — total, so the choice is deterministic."""
    score = filled = 0
    for col, val in row.items():
        if col in _NOT_CONTENT or val in (None, "", [], {}):
            continue
        filled += 1
        score += _RICHNESS_WEIGHTS.get(col, 1)
    return (score, filled, str(row.get("updated_at") or ""), str(row.get("slug") or ""))


def best_name_match(name: Optional[str], rows: Iterable[dict],
                    key: str = "name") -> Optional[dict]:
    """Return the row whose `key` best matches `name`, or None. Rows that
    match equally well (e.g. live duplicates) are ranked by entity_richness."""
    target = normalize_name(name or "")
    if not target:
        return None
    best, best_key = None, None
    for r in rows:
        cand = r.get(key)
        if not names_match(name, cand):
            continue
        score = SequenceMatcher(None, target, normalize_name(cand)).ratio()
        if normalize_name(cand) == target:
            score = 2.0
        rank = (score, entity_richness(r))
        if best_key is None or rank > best_key:
            best, best_key = r, rank
    return best


@dataclass
class LiveVenueMatch:
    kind: str                       # match | review | none
    venue: Optional[dict] = None    # set when kind == "match"
    candidates: list[dict] = field(default_factory=list)  # set when kind == "review"


def match_live_venue(name: str, live_venues: Iterable[dict]) -> LiveVenueMatch:
    """Decide which live TownLoops venue, if any, an event's venue name means.

    Only an exact normalized name or a close spelling of exactly one live name
    links. A name that only overlaps by tokens ("Gunbarrel" vs "Gunbarrel
    Tavern"), or that is close to several different live names, is a guess —
    `review` hands it to the operator rather than linking the wrong venue or
    shipping a duplicate shell. Same-name live duplicates resolve to the
    richest row (entity_richness)."""
    target = normalize_name(name or "")
    if not target:
        return LiveVenueMatch("none")
    live_venues = list(live_venues)
    rows = [v for v in live_venues if names_match(name, v.get("name"))]
    if not rows:
        # names_match ignores containment for short names ("Cuppa" in "Cuppa
        # Tahoe") to keep reconcile from linking loosely; for a new venue
        # that is still a likely duplicate, so it's a question, not a shell.
        tokens = set(target.split())
        rows = [v for v in live_venues
                if tokens <= set(normalize_name(v.get("name") or "").split())]
        if rows:
            return LiveVenueMatch("review", candidates=sorted(rows, key=lambda v: str(v.get("slug"))))
        return LiveVenueMatch("none")
    exact = [v for v in rows if normalize_name(v["name"]) == target]
    if exact:
        return LiveVenueMatch("match", max(exact, key=entity_richness))
    close = [v for v in rows if SequenceMatcher(
        None, target, normalize_name(v["name"])).ratio() >= NAME_MATCH_RATIO]
    if close and len({normalize_name(v["name"]) for v in close}) == 1:
        return LiveVenueMatch("match", max(close, key=entity_richness))
    return LiveVenueMatch("review", candidates=sorted(rows, key=lambda v: str(v.get("slug"))))


LIVE_VENUE_CACHE = _DB_PATH.parent / "cache" / "live-venues.json"
LIVE_VENUE_TTL_SECONDS = 600


def load_live_venues(fetch, cache_path: Path = LIVE_VENUE_CACHE,
                     ttl_seconds: int = LIVE_VENUE_TTL_SECONDS,
                     now: Optional[float] = None) -> list[dict]:
    """The live TownLoops venue list, fetched at most once per `ttl_seconds`.
    derive-venue runs as one subprocess per event, so the cache is a file;
    the TTL spans a workqueue drain. `fetch` raising propagates — the caller
    must not fall back to creating a shell it couldn't check."""
    now = time.time() if now is None else now
    try:
        cached = json.loads(cache_path.read_text())
        if now - float(cached["fetched_at"]) < ttl_seconds:
            return cached["venues"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    venues = list(fetch())
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": now, "venues": venues}))
        tmp.replace(cache_path)
    except OSError:
        pass
    return venues


def mint_entity_uid(kind: str, seed_name: str, event_uid: str) -> str:
    """Deterministic entity_uid so re-derivation of the same event doesn't
    spawn duplicate entities. `event_uid` is included so different events
    at the same venue still produce ONE venue row (the normalize_name
    lookup catches the collision before we ever call this)."""
    key = f"{kind}::{normalize_name(seed_name)}::{event_uid}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


# Fields copied from a live TownLoops row into its local mirror.
_MIRROR_FIELDS = ("name", "description", "address", "website", "phone", "kind",
                  "meeting_info", "image_path", "image_alt", "category_slug",
                  "instagram", "facebook", "tiktok", "twitter", "youtube", "linkedin")


def insert_entity(conn: sqlite3.Connection, table: str, entity_uid: str,
                  fields: dict) -> None:
    """INSERT OR IGNORE an entity row, keeping only columns the table has and
    filling the NOT NULL bookkeeping columns."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    now = iso_utc_now()
    base = {
        "entity_uid": entity_uid, "first_seen_at": now, "last_seen_at": now,
        "last_modified_at": now,
        "content_hash": hashlib.sha1(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest(),
        "linked_event_count": 0,
    }
    data = {k: v for k, v in {**base, **fields}.items() if k in cols and v is not None}
    conn.execute(
        f"INSERT OR IGNORE INTO {table} ({', '.join(data)}) VALUES ({', '.join('?' for _ in data)})",
        tuple(data.values()),
    )


def mirror_live_entity(conn: sqlite3.Connection, kind: str, live_row: dict) -> str:
    """Local stand-in for a row already live on TownLoops. event-ship sees
    source_kind='live' + townloops_slug and never POSTs it; the live id in
    extra_json lets a PATCH bind events to it by venue_id. Idempotent: the
    uid is keyed by the live slug."""
    entity_uid = mint_entity_uid(kind, live_row["name"], f"live:{live_row['slug']}")
    fields = {k: live_row.get(k) for k in _MIRROR_FIELDS}
    fields.update({
        "source": "live:townloops", "source_kind": "live",
        "townloops_slug": live_row["slug"], "pushed_at": iso_utc_now(),
        "extra_json": json.dumps({"live_id": live_row.get("id")}),
        "enrichment_status": "live",
    })
    insert_entity(conn, _entity_table(kind), entity_uid, fields)
    return entity_uid


def link_event_to_entity(conn: sqlite3.Connection, event_uid: str,
                         entity_kind: str, entity_uid: str,
                         role: str = "primary") -> None:
    """Idempotent link. Increments the entity's linked_event_count cache
    ONLY if this is a new link (SQLite doesn't have UPSERT RETURNING, so
    we check first)."""
    existing = conn.execute(
        "SELECT 1 FROM linked_events WHERE event_uid = ? AND entity_kind = ? "
        "AND entity_uid = ? AND role = ?",
        (event_uid, entity_kind, entity_uid, role),
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO linked_events(event_uid, entity_kind, entity_uid, role, linked_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_uid, entity_kind, entity_uid, role, iso_utc_now()),
    )
    table = _entity_table(entity_kind)
    if table:
        conn.execute(
            f"UPDATE {table} SET linked_event_count = COALESCE(linked_event_count, 0) + 1 "
            f"WHERE entity_uid = ?", (entity_uid,),
        )


def _entity_table(kind: str) -> Optional[str]:
    return {
        "venue": "venues",
        "business": "businesses",
        "community": "communities",
        "performer": "performers",
        "creator": "creators",
    }.get(kind)


def record_activity(conn: sqlite3.Connection, worker: str, action: str,
                    target: str, detail: str = "", ok: bool = True,
                    error: Optional[str] = None) -> None:
    """Write one row to agent_activity so the dashboard's live band shows
    what M3 skills are doing. Kept lightweight — skills call this often."""
    conn.execute(
        "INSERT INTO agent_activity(ts, worker, action, target, detail, "
        "  duration_ms, ok, error) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
        (iso_utc_now(), worker, action, target[:200], detail[:400],
         1 if ok else 0, error),
    )
