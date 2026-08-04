#!/usr/bin/env python3
"""
Town-agnostic events ingest — forked from the SLT skill's events.py.

Reads sources.yaml (SOURCES_YAML_PATH), fetches each configured source
with a structure-aware parser (iCal, JSON-LD, RSS, CSS selectors, or
web/Firecrawl), normalizes into a unified Event schema, dedupes, and
either dumps JSON (fetch) or upserts into a local SQLite (ingest).

Ships events + mined venues/businesses/communities/performers/creators
to TownLoops via ship / resync.

Fork changes:
  * TOWNLOOPS_TOWN_SLUG required from env — no `slt` default.
  * Paths (sources, DB, outbox, migrations) read from env with sensible
    defaults under HERMES_HOME/state/.
  * Timezone reads from AGENT_TZ.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal

import requests
import yaml

# Phase 2 of the fork extraction (2026-07-09) moved the parsers + fetch layer
# to engine/ingest_core.py. The upstream libs those parsers used
# (`feedparser`, `bs4`, `icalendar`, `dateutil.parser`, `dateutil.tz`,
# `unicodedata`, `urljoin`, `typing.Iterable`, `dataclasses.dataclass|field|fields`)
# are no longer referenced here — they're the shared core's dependencies.

# Shared ingest primitives — dataclasses, ENTITY_REG, pure utils, identity /
# content hashing, HTTP fetch, parsers. Extracted 2026-07-08 (Phase 1) and
# 2026-07-09 (Phase 2) to eliminate the drift-prone copy of these definitions
# between this repo and the SLT skill. See engine/ingest_core.py.
sys.path.insert(0, "/app")  # docker mount for engine/
from engine.ingest_core import (  # noqa: E402
    # Dataclasses
    Event, Venue, Business, Community, Performer, Creator, MinedEntity,
    # Registry
    ENTITY_REG, ENTITY_KINDS,
    # Pure utils
    _clean, _truncate, _norm,
    _venue_from_location, _extract_venue_from_description, _should_replace_venue,
    # Timezone helpers (DEFAULT_TZ picks up $AGENT_TZ — same source as legacy PT)
    DEFAULT_TZ as PT,
    _now_pt, _iso_now, _iso, _parse_dt,
    # Identity + content hashing
    _event_uid, _content_hash, _entity_uid, _entity_content_hash,
    # Regex constants callers reach for
    _HTML_TAG_RE,
    # Phase 2: HTTP fetch layer
    _http_get, _firecrawl_endpoint, _firecrawl_scrape,
    _detail_links_from_html,
    FIRECRAWL_CLOUD_BASE, DEFAULT_MAX_DETAIL_FETCHES,
    # Phase 2: JSON-LD + selector helpers
    _walk_jsonld, _jsonld_to_event, _extract_jsonld_from_html,
    _text, _attr_or_text, _extract_selectors_from_html,
    # Phase 2: top-level parsers + registry
    parse_ical, parse_jsonld, parse_rss, parse_selectors, parse_web,
    PARSERS,
    # Phase 2: enrichment overlay
    _extract_enrichment_field, _fetch_for_enrichment, _apply_enrichments,
)

SCRIPT_DIR = Path(__file__).resolve().parent

# --- Path config (env-var overridable) --------------------------------
# HERMES_HOME is the runtime state root; docker-compose points it at
# /home/agent/.hermes.
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
STATE_DIR = HERMES_HOME / "state"

SOURCES_PATH = Path(os.environ.get("SOURCES_YAML_PATH", STATE_DIR / "sources.yaml"))
DB_PATH = Path(os.environ.get("TOWNLOOPS_DB_PATH", STATE_DIR / "townloops.db"))
DATA_DIR = Path(os.environ.get("EVENTS_DATA_DIR", STATE_DIR / "events-data"))
OUTBOX_DIR = DATA_DIR / "outbox"

# Migrations live with the shared engine (mounted at /app/engine).
_ENGINE_MIG_DIR = Path(os.environ.get("ENGINE_MIGRATIONS_DIR", "/app/engine/migrations"))
MIGRATIONS_DIR = _ENGINE_MIG_DIR if _ENGINE_MIG_DIR.is_dir() else (SCRIPT_DIR / "migrations")

# Timezone: PT is aliased to engine.ingest_core.DEFAULT_TZ above (both derive
# from $AGENT_TZ, defaulting to America/Los_Angeles). Kept as the local name
# `PT` so legacy references throughout this file (parse_ical, etc.) are unchanged.

FIRECRAWL_CLOUD_BASE = "https://api.firecrawl.dev"
DEFAULT_MAX_DETAIL_FETCHES = 50

def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.stderr.write(
            f"FATAL: {name} env-var is required. "
            "Set it in .env before running any ingest command.\n"
        )
        raise SystemExit(2)
    return v


def _require_town_slug() -> str:
    return _require_env("TOWNLOOPS_TOWN_SLUG")


TOWNLOOPS_DEFAULT_BASE = _require_env("TOWNLOOPS_API_URL").rstrip("/") + "/api/v1"


TOWNLOOPS_TOWN_SLUG = _require_town_slug()
TOWNLOOPS_BULK_LIMIT = 100


# =====================================================================
# Event / entity dataclasses, ENTITY_REG, text/venue helpers, timezone
# helpers, identity + content hashing, HTTP fetch layer, JSON-LD walker,
# CSS-selector helpers, top-level parsers, enrichment overlay:
#   ALL EXTRACTED to engine/ingest_core.py (see the imports at the top).
# The extraction is DRIFT INSURANCE — keeping the ingest kernel in one
# place lets SLT's fork eventually adopt the shared core without a diff
# war. Phase 1 (2026-07-08): dataclasses + utils + hashing. Phase 2
# (2026-07-09): parsers + fetch layer.
# =====================================================================


def load_sources(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    return data.get("sources", []) or []


def _scrapeable_sources(sources: list[dict]) -> list[dict]:
    return [s for s in sources if not s.get("manual_only")]


def fetch_all(
    sources: list[dict],
    since: datetime,
    until: datetime | None,
    only: set[str] | None,
) -> list[Event]:
    """Fetch + dedupe + filter past events. No DB writes."""
    seen: dict[tuple, Event] = {}
    for src in _scrapeable_sources(sources):
        if only and src["name"] not in only:
            continue
        # `parser` is the canonical key; `type` is a legacy alias emitted by
        # older event-source-discovery compile output (2026-07-25 fix
        # standardized on `parser`). Accept both for one release cycle.
        parser_name = src.get("parser") or src.get("type")
        parser = PARSERS.get(parser_name)
        if not parser:
            print(f"[skip] {src['name']}: unknown parser {parser_name!r}", file=sys.stderr)
            continue
        try:
            events = parser(src)
        except Exception as exc:
            print(f"[error] {src['name']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        _apply_enrichments(events, src)
        kept = 0
        for ev in events:
            dt = _parse_dt(ev.date)
            if not dt:
                continue
            if dt < since:
                continue
            if until and dt > until:
                continue
            ev.date = _iso(dt)
            key = ev.dedupe_key()
            if key not in seen:
                seen[key] = ev
                kept += 1
        print(f"[ok] {src['name']}: {kept} future events", file=sys.stderr)
    return sorted(seen.values(), key=lambda e: e.date)


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row["filename"] for row in conn.execute("SELECT filename FROM schema_version")}
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
    for path in files:
        if path.name in applied:
            continue
        sql = path.read_text()
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version(filename, applied_at) VALUES(?, ?)",
                (path.name, _iso_now()),
            )
        print(f"[migrate] applied {path.name}", file=sys.stderr)


# _event_uid + _content_hash are imported from engine.ingest_core (extracted
# 2026-07-08). See the imports at the top of this file.


def _upsert_event(
    conn: sqlite3.Connection,
    ev: Event,
    source_kind: Literal["scraped", "manual"],
    extra: dict | None = None,
) -> Literal["inserted", "updated", "unchanged"]:
    uid = _event_uid(ev)
    chash = _content_hash(ev, extra)
    now = _iso_now()
    date_day = (ev.date or "")[:10]
    extra_json = json.dumps(extra) if extra else None

    row = conn.execute("SELECT content_hash FROM events WHERE event_uid = ?", (uid,)).fetchone()
    if row is None:
        with conn:
            conn.execute(
                """
                INSERT INTO events(
                    event_uid, source, source_kind, name, location, venue_name,
                    date, date_day, end_at, description, hosting_group,
                    source_url, extra_json, category_slug,
                    first_seen_at, last_seen_at, content_hash, last_modified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid, ev.source, source_kind, ev.name, ev.location, ev.venue_name or None,
                    ev.date, date_day, ev.end_at or None, ev.description, ev.hosting_group,
                    ev.source_url, extra_json, ev.category_slug or None,
                    now, now, chash, now,
                ),
            )
        return "inserted"
    if row["content_hash"] == chash:
        with conn:
            conn.execute(
                "UPDATE events SET last_seen_at = ? WHERE event_uid = ?",
                (now, uid),
            )
        return "unchanged"
    with conn:
        conn.execute(
            """
            UPDATE events SET
                source = ?, source_kind = ?, name = ?, location = ?, venue_name = ?,
                date = ?, date_day = ?, end_at = ?, description = ?, hosting_group = ?,
                source_url = ?, extra_json = ?, category_slug = ?, last_seen_at = ?,
                content_hash = ?, last_modified_at = ?
            WHERE event_uid = ?
            """,
            (
                ev.source, source_kind, ev.name, ev.location, ev.venue_name or None,
                ev.date, date_day, ev.end_at or None, ev.description, ev.hosting_group,
                ev.source_url, extra_json, ev.category_slug or None,
                now, chash, now, uid,
            ),
        )
    return "updated"


# =====================================================================
# Entity mining + upsert (mirror of _event_uid / _content_hash /
# _upsert_event but parameterized by ENTITY_REG kind).
# =====================================================================


# _entity_uid + _entity_content_hash are imported from engine.ingest_core
# (extracted 2026-07-08). See the imports at the top of this file.


def _upsert_entity(
    conn: sqlite3.Connection,
    kind: str,
    ent,
    source_kind: str = "mined",
) -> Literal["inserted", "updated", "unchanged"]:
    spec = ENTITY_REG[kind]
    table = spec["table"]
    cols = spec["cols"]
    uid = _entity_uid(kind, ent.name)
    chash = _entity_content_hash(kind, ent)
    now = _iso_now()
    extra_json = json.dumps(ent.extra) if getattr(ent, "extra", None) else None

    row = conn.execute(
        f"SELECT content_hash, source_kind FROM {table} WHERE entity_uid = ?", (uid,)
    ).fetchone()

    col_values = [getattr(ent, c, "") or None for c in cols]

    if row is None:
        placeholders = ",".join(["?"] * (len(cols) + 9))
        col_list = ", ".join(cols)
        with conn:
            conn.execute(
                f"""
                INSERT INTO {table}(
                    entity_uid, source, source_kind, {col_list},
                    source_url, extra_json,
                    first_seen_at, last_seen_at, content_hash, last_modified_at
                ) VALUES ({placeholders})
                """,
                (uid, ent.source, source_kind, *col_values,
                 ent.source_url or None, extra_json,
                 now, now, chash, now),
            )
        return "inserted"

    if row["source_kind"] == "manual" and source_kind != "manual":
        with conn:
            conn.execute(
                f"UPDATE {table} SET last_seen_at = ? WHERE entity_uid = ?",
                (now, uid),
            )
        return "unchanged"

    if row["content_hash"] == chash:
        with conn:
            conn.execute(
                f"UPDATE {table} SET last_seen_at = ? WHERE entity_uid = ?",
                (now, uid),
            )
        return "unchanged"

    set_clauses = ", ".join([f"{c} = ?" for c in cols])
    with conn:
        conn.execute(
            f"""
            UPDATE {table} SET
                source = ?, source_kind = ?, {set_clauses},
                source_url = ?, extra_json = ?,
                last_seen_at = ?, content_hash = ?, last_modified_at = ?
            WHERE entity_uid = ?
            """,
            (ent.source, source_kind, *col_values,
             ent.source_url or None, extra_json,
             now, chash, now, uid),
        )
    return "updated"


# =====================================================================
# Mining policy — convert one parsed Event + its source config into
# zero-or-more MinedEntity(kind=..., data=...). Centralizes all "is
# this a business or a community" decisions.
# =====================================================================


def _derive_venue_address(location: str, venue_name: str) -> str:
    """Derive a clean address from full Event.location by stripping a
    leading venue-name prefix if present. Casefold-aware."""
    if not location:
        return ""
    if not venue_name:
        return location
    loc_cf = location.casefold()
    vn_cf = venue_name.casefold()
    if loc_cf == vn_cf:
        return ""  # just a venue name, no street info
    if loc_cf.startswith(vn_cf + ","):
        return location[len(venue_name) + 1:].lstrip(", ").strip()
    if loc_cf.startswith(vn_cf + " ,"):
        return location[len(venue_name) + 2:].lstrip(", ").strip()
    return location


def _mine_entities(ev: Event, source: dict) -> list[MinedEntity]:
    """Emit zero-or-more typed entities derived from a single event."""
    out: list[MinedEntity] = []
    mx = ev.mining_extras or {}

    # ---- 1. Venue ----
    skip_names = {_norm(n) for n in (source.get("skip_venue_names") or [])}
    default_loc_norm = _norm(source.get("default_location", ""))
    venue_name_norm = _norm(ev.venue_name)
    if ev.venue_name and venue_name_norm not in skip_names and venue_name_norm != default_loc_norm:
        out.append(MinedEntity(
            kind="venue",
            data=Venue(
                name=ev.venue_name,
                address=_derive_venue_address(ev.location, ev.venue_name),
                source=ev.source,
                source_url=ev.source_url,
            ),
        ))

    # ---- 2. Organization (business or community) ----
    org_kind = source.get("default_organization_kind", "skip")
    if org_kind in ("business", "community"):
        org_extra = mx.get("organizer") or {}
        org_name = (org_extra.get("name") or "").strip() or (ev.hosting_group or "").strip()
        if org_name:
            org_url = (org_extra.get("url") or "").strip()
            if org_kind == "business":
                out.append(MinedEntity(
                    kind="business",
                    data=Business(
                        name=org_name,
                        website=org_url,
                        source=ev.source,
                        source_url=ev.source_url,
                    ),
                ))
            else:
                out.append(MinedEntity(
                    kind="community",
                    data=Community(
                        name=org_name,
                        website=org_url,
                        source=ev.source,
                        source_url=ev.source_url,
                    ),
                ))

    # ---- 3. Performers ----
    for p in mx.get("performers", []):
        nm = (p.get("name") or "").strip()
        if not nm:
            continue
        out.append(MinedEntity(
            kind="performer",
            data=Performer(
                name=nm,
                kind="band",          # Townloops default; UI re-categorizes
                website=(p.get("url") or "").strip(),
                source=ev.source,
                source_url=ev.source_url,
                extra={"jsonld_type": p.get("type", "")} if p.get("type") else {},
            ),
        ))

    # ---- 4. Creators ----
    for c in mx.get("creators", []):
        nm = (c.get("name") or "").strip()
        if not nm:
            continue
        out.append(MinedEntity(
            kind="creator",
            data=Creator(
                name=nm,
                kind="other",
                website=(c.get("url") or "").strip(),
                source=ev.source,
                source_url=ev.source_url,
                extra={"jsonld_type": c.get("type", "")} if c.get("type") else {},
            ),
        ))

    return out


# ---------------------------------------------------------------------
# Subcommand helpers
# ---------------------------------------------------------------------


def _canonical_event_keys(conn: sqlite3.Connection, canonical_source_names: set[str]) -> set:
    """Return (_norm(name), date_day) tuples for every event already in
    DB belonging to a canonical source. Used to suppress syndicator dupes."""
    if not canonical_source_names:
        return set()
    placeholders = ",".join(["?"] * len(canonical_source_names))
    rows = conn.execute(
        f"SELECT name, date_day FROM events WHERE source IN ({placeholders})",
        list(canonical_source_names),
    ).fetchall()
    return {(_norm(r["name"]), r["date_day"]) for r in rows}


def _ingest_source(
    conn: sqlite3.Connection,
    src: dict,
    canonical_keys: set | None = None,
) -> dict:
    started = _iso_now()
    fetched = inserted = updated = suppressed = 0
    ok = 1
    err: str | None = None
    parser_name = src.get("parser")
    parser = PARSERS.get(parser_name)
    if not parser:
        ok = 0
        err = f"unknown parser {parser_name!r}"
    else:
        try:
            events = parser(src)
        except Exception as exc:
            ok = 0
            err = f"{type(exc).__name__}: {exc}"
            events = []
        if events:
            try:
                _apply_enrichments(events, src)
            except Exception as exc:
                print(f"[warn] {src['name']} enrichment: {type(exc).__name__}: {exc}", file=sys.stderr)
        fetched = len(events)
        # Suppress syndicator's events whose (name, date_day) match any
        # canonical-source event already in DB. Avoids future dupes when
        # Big Blue Adventure / aggregators re-list canonical race series.
        is_syndicator = src.get("source_role") == "syndicator"
        ck = canonical_keys if (is_syndicator and canonical_keys) else None
        # Dedupe within the source's returned list before upserting.
        local_seen: dict[tuple, Event] = {}
        for ev in events:
            dt = _parse_dt(ev.date)
            if not dt:
                continue
            if dt < _now_pt():
                continue
            ev.date = _iso(dt)
            if ck and (_norm(ev.name), ev.date[:10]) in ck:
                suppressed += 1
                continue
            key = ev.dedupe_key()
            if key not in local_seen:
                local_seen[key] = ev
        # Track per-kind entity actions so we can roll up into source_runs.
        ec: dict = {k: {"inserted": 0, "updated": 0, "unchanged": 0} for k in ENTITY_KINDS}
        for ev in local_seen.values():
            action = _upsert_event(conn, ev, "scraped")
            if action == "inserted":
                inserted += 1
            elif action == "updated":
                updated += 1
            for mined in _mine_entities(ev, src):
                try:
                    a = _upsert_entity(conn, mined.kind, mined.data)
                    ec[mined.kind][a] += 1
                except Exception as exc:
                    print(f"[warn] {src['name']} mine {mined.kind}: {type(exc).__name__}: {exc}", file=sys.stderr)
    ended = _iso_now()
    entity_counts_json = json.dumps(ec) if 'ec' in locals() and any(v != {"inserted":0,"updated":0,"unchanged":0} for v in ec.values()) else None
    with conn:
        conn.execute(
            """
            INSERT INTO source_runs(source, started_at, ended_at, ok, fetched, inserted, updated, error, entity_counts_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (src["name"], started, ended, ok, fetched, inserted, updated, err, entity_counts_json),
        )
    status = "[ok]" if ok else "[error]"
    suffix = f" — {err}" if err else ""
    entity_summary = ""
    if 'ec' in locals():
        bits = []
        for k in ENTITY_KINDS:
            tot_i = ec[k]["inserted"]
            tot_u = ec[k]["updated"]
            if tot_i or tot_u:
                # Use the registry's endpoint field for correct pluralization
                # (avoids "businesss" / "communitys"). Reads as venues/businesses/etc.
                bits.append(f"{ENTITY_REG[k]['endpoint']}={tot_i}+{tot_u}u")
        if bits:
            entity_summary = "  entities: " + ", ".join(bits)
    suppress_note = f" suppressed={suppressed}" if suppressed else ""
    print(
        f"{status} {src['name']}: fetched={fetched} inserted={inserted} updated={updated}{suppress_note}{suffix}{entity_summary}",
        file=sys.stderr,
    )
    return {
        "source": src["name"],
        "ok": bool(ok),
        "fetched": fetched,
        "inserted": inserted,
        "updated": updated,
        "error": err,
    }


def _select_events(
    conn: sqlite3.Connection,
    since: datetime | None = None,
    until: datetime | None = None,
    source: str | None = None,
    source_kind: str | None = None,
    unpushed: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE 1=1"
    params: list = []
    if since:
        sql += " AND date_day >= ?"
        params.append(since.date().isoformat())
    if until:
        sql += " AND date_day <= ?"
        params.append(until.date().isoformat())
    if source:
        sql += " AND source = ?"
        params.append(source)
    if source_kind:
        sql += " AND source_kind = ?"
        params.append(source_kind)
    if unpushed:
        sql += " AND (pushed_at IS NULL OR pushed_hash != content_hash)"
    sql += " ORDER BY date_day, date"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def _row_to_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    if out.get("extra_json"):
        try:
            out["extra"] = json.loads(out["extra_json"])
        except (json.JSONDecodeError, TypeError):
            out["extra"] = {}
    else:
        out["extra"] = {}
    del out["extra_json"]
    return out


def _row_to_event_payload(row: sqlite3.Row) -> dict:
    """Build the EventPayload that POST /events and PATCH /events/<slug> both consume."""
    payload = {
        "title": row["name"],
        "start_at": row["date"],
    }
    if row["description"]:
        payload["description"] = row["description"]
    if row["end_at"]:
        payload["end_at"] = row["end_at"]
    venue = row["venue_name"] or _venue_from_location(row["location"] or "")
    if venue:
        # API does case-insensitive venue_name lookup within the town; a miss
        # becomes a `venue_not_found` warning (event still saves).
        payload["venue_name"] = venue
    if row["category_slug"]:
        payload["category_slug"] = row["category_slug"]
    if row["source_url"]:
        payload["source_url"] = row["source_url"]
    return payload


def _row_to_townloops_entry(row: sqlite3.Row) -> dict:
    """Shape a row for POST /towns/<slug>/entries[/bulk] (Townloops Ingest API).

    `slug` is town-scoped so re-ships hit `409 slug_taken`, which the ship
    loop treats as 'already shipped', AND so eyeballing an entries dump
    tells you which town a row belongs to at a glance.
    """
    payload = _row_to_event_payload(row)
    payload["slug"] = _slug_for_new_post(row, "event")
    return {
        "entity_type": "event",
        "payload": payload,
        "metadata": {
            "source": row["source"],
            "source_kind": row["source_kind"],
            "hermes_event_uid": row["event_uid"],
        },
    }


# =====================================================================
# Kind-generic ship helpers.
# =====================================================================


def _row_uid(row: sqlite3.Row, kind: str) -> str:
    return row["event_uid"] if kind == "event" else row["entity_uid"]


def _slug_for_new_post(row: sqlite3.Row, kind: str) -> str:
    """Deterministic slug we PROPOSE on first POST. Town-scoped so we can
    read a slug and immediately know which town it belongs to (helps when
    debugging cross-town leaks or when eyeballing a bulk endpoint's output).

    Townloops may accept as-is or rewrite on collision; `_capture_townloops_slug`
    records whichever wins. PATCHes use the captured value, not this.
    """
    return f"hermes-{TOWNLOOPS_TOWN_SLUG}-{_row_uid(row, kind)[:16]}"


def _slug_for_kind(row: sqlite3.Row, kind: str) -> str:
    """LEGACY-COMPATIBLE deterministic slug — the pre-2026-07-08 format
    without a town prefix. Retained ONLY as the PATCH-URL fallback for
    rows that predate the slug-capture feature. New POSTs use the
    town-prefixed `_slug_for_new_post` — see it for the rationale.
    """
    return f"hermes-{_row_uid(row, kind)[:16]}"


def _patch_target_slug(row: sqlite3.Row, kind: str) -> str:
    """Slug to use in the PATCH URL: prefer the actual Townloops slug
    captured at POST/PATCH time, fall back to the pre-town-prefix
    deterministic form for rows that predate slug capture."""
    try:
        actual = row["townloops_slug"]
    except (IndexError, KeyError):
        actual = None
    return actual or _slug_for_kind(row, kind)


def _capture_townloops_slug(
    conn: sqlite3.Connection, kind: str, uid_col: str, uid: str, slug: str,
) -> None:
    """Persist the slug Townloops returned for this row, so subsequent
    PATCH requests target the correct path even if Townloops rewrote it."""
    if not slug:
        return
    table = _ship_table_for_kind(kind)
    with conn:
        conn.execute(
            f"UPDATE {table} SET townloops_slug = ? WHERE {uid_col} = ?",
            (slug, uid),
        )


def _select_unpushed_by_kind(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    if kind == "event":
        return _select_events(conn, unpushed=True)
    table = ENTITY_REG[kind]["table"]
    return list(conn.execute(
        f"SELECT * FROM {table} "
        f"WHERE pushed_at IS NULL OR pushed_hash != content_hash"
    ))


def _row_to_entity_payload(row: sqlite3.Row, kind: str) -> dict:
    """Build the per-kind Townloops payload from a DB row."""
    spec = ENTITY_REG[kind]
    payload = {}
    for col in spec["cols"]:
        val = row[col]
        if val:
            payload[col] = val
    return payload


def _row_to_bulk_entry(row: sqlite3.Row, kind: str) -> dict:
    """Build a /entries/bulk envelope item for any kind."""
    if kind == "event":
        return _row_to_townloops_entry(row)
    payload = _row_to_entity_payload(row, kind)
    payload["slug"] = _slug_for_new_post(row, kind)
    return {
        "entity_type": kind,
        "payload": payload,
        "metadata": {
            "source": row["source"],
            "source_kind": row["source_kind"],
            "hermes_entity_uid": row["entity_uid"],
        },
    }


def _patch_payload_for_kind(row: sqlite3.Row, kind: str) -> dict:
    """PATCH body for the kind (no slug — not patchable per API spec)."""
    if kind == "event":
        return _row_to_event_payload(row)
    return _row_to_entity_payload(row, kind)


def _ship_table_for_kind(kind: str) -> str:
    return "events" if kind == "event" else ENTITY_REG[kind]["table"]


def _patch_endpoint_for_kind(base: str, town_slug: str, kind: str, item_slug: str) -> str:
    seg = "events" if kind == "event" else ENTITY_REG[kind]["endpoint"]
    return f"{base}/towns/{town_slug}/{seg}/{item_slug}"


def _bulk_post(
    conn: sqlite3.Connection, kind: str, rows: list[sqlite3.Row],
    bulk_endpoint: str, api_key: str, batch_size: int, dry_run: bool,
) -> tuple[int, int, list[dict]]:
    """POST new rows in batches. Returns (shipped, failed, failures)."""
    shipped = failed = 0
    failures: list[dict] = []
    table = _ship_table_for_kind(kind)
    uid_col = "event_uid" if kind == "event" else "entity_uid"
    name_col = "name" if kind == "event" else "name"

    def _mark(row, now):
        with conn:
            conn.execute(
                f"UPDATE {table} SET pushed_at = ?, pushed_hash = ? WHERE {uid_col} = ?",
                (now, row["content_hash"], row[uid_col]),
            )

    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        body = [_row_to_bulk_entry(r, kind) for r in chunk]
        if dry_run:
            print(f"--- POST {bulk_endpoint} (kind={kind}, {len(chunk)} entries) ---")
            print(json.dumps(body, indent=2))
            shipped += len(chunk)
            continue
        # Deterministic idempotency key: hash of sorted uids in the batch,
        # town-scoped. Retry of the exact same batch replays server-side
        # (safe); a different batch (rows added/removed) is a distinct op.
        chunk_sig = hashlib.sha1(
            ("|".join(sorted(r[uid_col] for r in chunk))).encode("utf-8")
        ).hexdigest()[:20]
        try:
            resp = requests.post(
                bulk_endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": f"hermes-{TOWNLOOPS_TOWN_SLUG}:{kind}:post:{chunk_sig}",
                },
                json=body,
                timeout=60,
            )
        except requests.RequestException as exc:
            print(f"[error] POST {kind} network: {exc}", file=sys.stderr)
            failed += len(chunk)
            failures.append({"kind": kind, "http": None, "error": str(exc), "count": len(chunk)})
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "30"))
            print(f"[rate_limited] POST {kind}: sleeping {wait}s, leaving chunk for next run", file=sys.stderr)
            time.sleep(min(wait, 120))
            failed += len(chunk)
            continue
        if resp.status_code in (401, 403):
            print(f"[fatal] POST {kind} {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
            raise SystemExit(1)
        if resp.status_code != 207:
            print(f"[error] POST {kind} unexpected status {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
            failed += len(chunk)
            failures.append({"kind": kind, "http": resp.status_code, "body": resp.text[:300]})
            continue
        results = resp.json().get("data") or []
        now = _iso_now()
        for idx, item in enumerate(results):
            row = chunk[idx]
            err = item.get("error") or {}
            err_code = err.get("code", "") if isinstance(err, dict) else ""
            if item.get("ok") or err_code == "slug_taken":
                _mark(row, now)
                # Capture the slug Townloops actually used (it may differ
                # from our `hermes-<hash>` if Townloops auto-renamed on
                # collision, or just returns what we sent on success).
                returned = ((item.get("data") or {}).get("slug")) or ""
                if returned:
                    _capture_townloops_slug(conn, kind, uid_col, row[uid_col], returned)
                shipped += 1
            else:
                failed += 1
                failures.append({"kind": kind, "uid": row[uid_col], "name": row[name_col], "error": err})
    return shipped, failed, failures


def _patch_one(
    conn: sqlite3.Connection, kind: str, row: sqlite3.Row,
    base: str, town_slug: str, api_key: str, dry_run: bool,
) -> tuple[int, int, list[dict]]:
    """PATCH one updated row. Returns (shipped, failed, failures)."""
    table = _ship_table_for_kind(kind)
    uid_col = "event_uid" if kind == "event" else "entity_uid"
    name_col = "name" if kind == "event" else "name"
    # Use the captured Townloops slug if we have one (handles Townloops-side
    # slug renames); fall back to our deterministic hermes-<hash> for legacy
    # rows that haven't been resync'd yet.
    item_slug = _patch_target_slug(row, kind)
    url = _patch_endpoint_for_kind(base, town_slug, kind, item_slug)
    payload = _patch_payload_for_kind(row, kind)
    if dry_run:
        print(f"--- PATCH {url} ---")
        print(json.dumps(payload, indent=2))
        return 1, 0, []
    try:
        resp = requests.patch(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # Deterministic on content_hash: retries dedupe server-side.
                # Ship logic upstream only PATCHes when content_hash changed,
                # so a different key naturally means a distinct edit.
                "Idempotency-Key": f"hermes-{TOWNLOOPS_TOWN_SLUG}:{kind}:patch:{row['content_hash']}",
            },
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        return 0, 1, [{"kind": kind, "uid": row[uid_col], "error": str(exc)}]
    if resp.status_code in (200, 201):
        with conn:
            conn.execute(
                f"UPDATE {table} SET pushed_at = ?, pushed_hash = ? WHERE {uid_col} = ?",
                (_iso_now(), row["content_hash"], row[uid_col]),
            )
        # Re-capture in case the PATCH response reflects a slug rename
        # (rare but possible — Townloops may normalize on edit).
        returned = ((resp.json().get("data") or {}).get("slug")) if resp.content else ""
        if returned and returned != item_slug:
            _capture_townloops_slug(conn, kind, uid_col, row[uid_col], returned)
        return 1, 0, []
    if resp.status_code == 404:
        with conn:
            conn.execute(
                f"UPDATE {table} SET pushed_at = NULL, pushed_hash = NULL, townloops_slug = NULL WHERE {uid_col} = ?",
                (row[uid_col],),
            )
        print(f"[reset] PATCH {kind} 404 {row[name_col][:50]} — will re-POST next ship", file=sys.stderr)
        return 0, 1, [{"kind": kind, "uid": row[uid_col], "name": row[name_col],
                       "error": {"code": "not_found_resetting", "hint": "next ship will re-POST"}}]
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", "30"))
        print(f"[rate_limited] PATCH {kind}: sleeping {wait}s", file=sys.stderr)
        time.sleep(min(wait, 120))
        return 0, 1, []
    if resp.status_code in (401, 403):
        print(f"[fatal] PATCH {kind} {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[fail] PATCH {kind} {resp.status_code} {row[name_col][:50]}", file=sys.stderr)
    return 0, 1, [{"kind": kind, "uid": row[uid_col], "http": resp.status_code, "body": resp.text[:300]}]


def _row_to_payload(row: sqlite3.Row) -> dict:
    """Shape a row for the outbox batch envelope."""
    return {
        "idempotency_key": row["event_uid"],
        "content_hash": row["content_hash"],
        "source": row["source"],
        "source_kind": row["source_kind"],
        "source_url": row["source_url"] or "",
        "name": row["name"],
        "hosting_group": row["hosting_group"] or "",
        "location": row["location"] or "",
        "venue_name": row["venue_name"] or "",
        "date": row["date"],
        "end_at": row["end_at"] or "",
        "date_day": row["date_day"],
        "description": row["description"] or "",
        "category_slug": row["category_slug"] or "",
        "first_seen_at": row["first_seen_at"],
        "last_modified_at": row["last_modified_at"],
        "extra": json.loads(row["extra_json"]) if row["extra_json"] else {},
    }


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def _cmd_fetch(args) -> int:
    sources = load_sources(Path(args.sources))
    since = _parse_dt(args.since) if args.since else _now_pt()
    until = _parse_dt(args.until) if args.until else None
    only = set(s.strip() for s in args.only.split(",")) if args.only else None
    events = fetch_all(sources, since, until, only)
    # Strip mining_extras from JSON output — it's an internal pipeline field
    # and would surprise downstream `events.py fetch | jq` consumers.
    payload = [{k: v for k, v in asdict(e).items() if k != "mining_extras"} for e in events]
    text = json.dumps(payload, separators=(",", ":")) if args.compact else json.dumps(payload, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    else:
        print(text)
    return 0


def _cmd_list_sources(args) -> int:
    for s in load_sources(Path(args.sources)):
        flag = "[manual]" if s.get("manual_only") else f"[{s.get('parser', '?')}]"
        print(f"{s.get('name', '<unnamed>'):<45} {flag:<12} {s.get('url', '')}")
    return 0


def _cmd_validate(args) -> int:
    sources = load_sources(Path(args.sources))
    since = _parse_dt(args.since) if args.since else _now_pt()
    events = fetch_all(sources, since, None, None)
    print(f"Total future events: {len(events)}", file=sys.stderr)
    return 0


def _cmd_ingest(args) -> int:
    # Lock so tomorrow's 09:00 cron doesn't start while yesterday's is hung
    # at hour 22. Hermes' max_parallel_jobs gates parallel triggers but not
    # stuck-job re-entry. Exit 0 on collision — this is expected, not failure.
    try:
        from engine import lock as _lock
        _lock_cm = _lock.acquire(f"event-ingest-{TOWNLOOPS_TOWN_SLUG}")
    except Exception as exc:
        print(f"[event-ingest] lock module import failed: {exc}", file=sys.stderr)
        raise
    try:
        with _lock_cm:
            return _cmd_ingest_locked(args)
    except _lock.LockHeld as e:
        print(f"[event-ingest] previous ingest still running ({e}); skipping.",
              file=sys.stderr)
        print(json.dumps({"skipped": True, "reason": str(e)}))
        return 0


def _cmd_ingest_locked(args) -> int:
    # Cross-town guard: refuse to run if $TOWNLOOPS_TOWN_SLUG's live categories
    # don't include everything in state/categories.json. Prevents a misconfigured
    # env from silently POSTing SLT events into another town (or vice versa).
    try:
        from engine import townloops_api as _tlapi
        _tlapi.Client().verify_town()
    except Exception as exc:
        print(f"[event-ingest] cross-town guard failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
    sources = load_sources(Path(args.sources))
    conn = connect()
    apply_migrations(conn)
    only = set(s.strip() for s in args.only.split(",")) if args.only else None
    if args.dry_run:
        # Run fetch_all without writing anything; still reports per-source counts.
        fetch_all(sources, _now_pt(), None, only)
        return 0
    # Build the canonical-keys set BEFORE the loop so syndicators ingested in
    # the same pass don't accidentally race against canonicals not-yet-upserted.
    # For manual_only canonical sources (the common case for awkwardly-walled
    # upstream sites like tahoeswimming.com), the events.db source is
    # 'manual:<slug>' — include that variant too.
    canonical_names: set[str] = set()
    for s in sources:
        if s.get("source_role") != "canonical":
            continue
        canonical_names.add(s["name"])
        if s.get("manual_only"):
            guess_slug = re.sub(r"[^a-z0-9]+", "-", s["name"].lower()).strip("-")
            canonical_names.add(f"manual:{guess_slug}")
    canonical_keys = _canonical_event_keys(conn, canonical_names)
    results: list[dict] = []
    for src in _scrapeable_sources(sources):
        if only and src["name"] not in only:
            continue
        results.append(_ingest_source(conn, src, canonical_keys=canonical_keys))
    total_inserted = sum(r["inserted"] for r in results)
    total_updated = sum(r["updated"] for r in results)
    total_unpushed = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE pushed_at IS NULL OR pushed_hash != content_hash"
    ).fetchone()["c"]
    failed = [r["source"] for r in results if not r["ok"]]
    print(
        json.dumps(
            {
                "ingested": total_inserted,
                "updated": total_updated,
                "unpushed_total": total_unpushed,
                "failed_sources": failed,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _slug_to_source(slug: str) -> str:
    return f"manual:{slug}"


def _cmd_add_manual(args) -> int:
    if args.from_stdin_json:
        payload = json.loads(sys.stdin.read())
        slug = args.slug or payload.get("slug")
        if not slug:
            print("[error] add-manual: --slug or 'slug' in JSON is required", file=sys.stderr)
            return 2
        ev = Event(
            name=payload["name"],
            location=payload.get("location", ""),
            venue_name=payload.get("venue_name", ""),
            date=payload["date"],
            end_at=payload.get("end_at", ""),
            description=payload.get("description", ""),
            hosting_group=payload.get("hosting_group") or _slug_to_source(slug),
            source=_slug_to_source(slug),
            source_url=payload.get("source_url", ""),
            category_slug=payload.get("category_slug", ""),
        )
        extra = payload.get("extra") or None
    else:
        if not (args.name and args.date and args.slug):
            print("[error] add-manual: --name, --date, and --slug are required", file=sys.stderr)
            return 2
        ev = Event(
            name=args.name,
            location=args.location or "",
            venue_name=args.venue_name or "",
            date=args.date,
            end_at=args.end_at or "",
            description=args.description or "",
            hosting_group=args.hosting_group or _slug_to_source(args.slug),
            source=_slug_to_source(args.slug),
            source_url=args.source_url or "",
            category_slug=args.category_slug or "",
        )
        extra = None
    dt = _parse_dt(ev.date)
    if dt is None:
        print(f"[error] add-manual: could not parse --date {ev.date!r}", file=sys.stderr)
        return 2
    ev.date = _iso(dt)
    conn = connect()
    apply_migrations(conn)
    action = _upsert_event(conn, ev, "manual", extra)
    # Manual events don't carry a sources.yaml entry, so mining sees no
    # default_organization_kind (skip) and no default_location skip-list.
    # Only the venue (from ev.venue_name) gets mined — which is the common
    # case operators care about for `add-manual --venue-name`.
    mined_entities = _mine_entities(ev, {})
    mined_counts: dict = {}
    for mined in mined_entities:
        a = _upsert_entity(conn, mined.kind, mined.data, source_kind="manual")
        mined_counts[mined.kind] = mined_counts.get(mined.kind, 0) + (1 if a in ("inserted", "updated") else 0)
    print(json.dumps({"action": action, "event_uid": _event_uid(ev),
                      "entities": mined_counts}, separators=(",", ":")))
    return 0


def _cmd_import_manual(args) -> int:
    path = Path(args.file)
    text = path.read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if isinstance(data, dict) and "events" in data:
        items = data["events"]
    elif isinstance(data, list):
        items = data
    else:
        print("[error] import-manual: file must be a list or {events: [...]} mapping", file=sys.stderr)
        return 2
    conn = connect()
    apply_migrations(conn)
    inserted = updated = unchanged = skipped = 0
    entity_inserts: dict = {}
    for item in items:
        slug = item.get("slug")
        if not (slug and item.get("name") and item.get("date")):
            skipped += 1
            continue
        dt = _parse_dt(item["date"])
        if dt is None:
            skipped += 1
            continue
        ev = Event(
            name=item["name"],
            location=item.get("location", ""),
            venue_name=item.get("venue_name", ""),
            date=_iso(dt),
            end_at=item.get("end_at", ""),
            description=item.get("description", ""),
            hosting_group=item.get("hosting_group") or _slug_to_source(slug),
            source=_slug_to_source(slug),
            source_url=item.get("source_url", ""),
            category_slug=item.get("category_slug", ""),
        )
        action = _upsert_event(conn, ev, "manual", item.get("extra") or None)
        if action == "inserted":
            inserted += 1
        elif action == "updated":
            updated += 1
        else:
            unchanged += 1
        for mined in _mine_entities(ev, {}):
            entity_inserts[mined.kind] = entity_inserts.get(mined.kind, 0) + 1
            _upsert_entity(conn, mined.kind, mined.data, source_kind="manual")
    print(
        json.dumps(
            {
                "inserted": inserted,
                "updated": updated,
                "unchanged": unchanged,
                "skipped": skipped,
                "entities": entity_inserts,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _cmd_list(args) -> int:
    conn = connect()
    apply_migrations(conn)
    kind = args.kind
    if kind == "event":
        since = _parse_dt(args.since) if args.since else None
        until = _parse_dt(args.until) if args.until else None
        rows = _select_events(
            conn,
            since=since,
            until=until,
            source=args.source,
            source_kind=args.source_kind,
            unpushed=args.unpushed,
            limit=args.limit,
        )
        if args.table:
            for r in rows:
                print(f"{r['date_day']}  {r['source'][:30]:<30}  {r['name'][:60]}")
            print(f"[{len(rows)} rows]", file=sys.stderr)
            return 0
        out = [_row_to_dict(r) for r in rows]
        print(json.dumps(out, indent=2, default=str))
        return 0

    # Entity kinds: query the kind's table.
    table = ENTITY_REG[kind]["table"]
    sql = f"SELECT * FROM {table} WHERE 1=1"
    params: list = []
    if args.source:
        sql += " AND source = ?"
        params.append(args.source)
    if args.source_kind:
        sql += " AND source_kind = ?"
        params.append(args.source_kind)
    if args.unpushed:
        sql += " AND (pushed_at IS NULL OR pushed_hash != content_hash)"
    sql += " ORDER BY name"
    if args.limit:
        sql += " LIMIT ?"
        params.append(args.limit)
    rows = list(conn.execute(sql, params))
    if args.table:
        for r in rows:
            extra_col = r["address"] if "address" in r.keys() and r["address"] else (
                r["website"] if "website" in r.keys() else "")
            print(f"{r['source'][:25]:<25}  {r['name'][:40]:<40}  {(extra_col or '')[:40]}")
        print(f"[{len(rows)} rows, kind={kind}]", file=sys.stderr)
        return 0
    out = [dict(r) for r in rows]
    print(json.dumps(out, indent=2, default=str))
    return 0


def _cmd_kinds(args) -> int:
    """Health-check view: per-kind row counts and unpushed counts."""
    conn = connect()
    apply_migrations(conn)
    out: dict = {}
    out["event"] = {
        "total": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "unpushed": conn.execute(
            "SELECT COUNT(*) FROM events WHERE pushed_at IS NULL OR pushed_hash != content_hash"
        ).fetchone()[0],
    }
    for kind in ENTITY_KINDS:
        table = ENTITY_REG[kind]["table"]
        out[kind] = {
            "total": conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
            "unpushed": conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE pushed_at IS NULL OR pushed_hash != content_hash"
            ).fetchone()[0],
        }
    print(json.dumps(out, indent=2))
    return 0


def _cmd_flush(args) -> int:
    conn = connect()
    apply_migrations(conn)
    kind = args.kind
    rows = _select_unpushed_by_kind(conn, kind)
    if not rows:
        print(json.dumps({"kind": kind, "unpushed_total": 0, "batches_written": 0}, separators=(",", ":")))
        return 0
    batch_size = max(1, args.batch_size)
    batches_written = 0
    pushed_uids: list[tuple[str, str]] = []
    table = _ship_table_for_kind(kind)
    uid_col = "event_uid" if kind == "event" else "entity_uid"
    payload_builder = _row_to_payload if kind == "event" else (lambda r: _row_to_entity_payload(r, kind))
    container_key = "events" if kind == "event" else f"{kind}s"
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        batch_id = uuid.uuid4().hex
        payload = {
            "batch_id": batch_id,
            "generated_at": _iso_now(),
            "agent": f"hermes/{TOWNLOOPS_TOWN_SLUG}-events",
            "schema_version": 1,
            "kind": kind,
            container_key: [payload_builder(r) for r in chunk],
        }
        if args.dry_run:
            print(json.dumps(payload, indent=2))
        else:
            out_path = OUTBOX_DIR / f"{kind}-{batch_id}.json"
            out_path.write_text(json.dumps(payload, indent=2))
            for r in chunk:
                pushed_uids.append((r[uid_col], r["content_hash"]))
        batches_written += 1
    if not args.dry_run and pushed_uids:
        now = _iso_now()
        with conn:
            conn.executemany(
                f"UPDATE {table} SET pushed_at = ?, pushed_hash = ? WHERE {uid_col} = ?",
                [(now, ch, uid) for (uid, ch) in pushed_uids],
            )
    print(
        json.dumps(
            {
                "kind": kind,
                "unpushed_total": len(rows),
                "batches_written": batches_written,
                "dry_run": bool(args.dry_run),
            },
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )
    return 0


def _cmd_ship(args) -> int:
    """Ship unpushed events + entities to Townloops.

    Iterates over all kinds (event + 5 entity types). Per-kind:
      - new rows (pushed_at IS NULL)        → bulk POST /entries/bulk
      - changed rows (pushed_hash != content_hash) → individual PATCH /<kind-endpoint>/<slug>
    """
    conn = connect()
    apply_migrations(conn)

    api_key = os.getenv("TOWNLOOPS_API_KEY", "").strip()
    if not api_key and not args.dry_run:
        print("[error] TOWNLOOPS_API_KEY not set in env", file=sys.stderr)
        return 2
    # TOWNLOOPS_DEFAULT_BASE already carries the /api/v1 segment; using the raw
    # env var here would drop it (the env var is always set, so the old `or`
    # short-circuit never reached the suffixed default). Matches townloops_api._url().
    base = TOWNLOOPS_DEFAULT_BASE.rstrip("/")
    town_slug = args.town or TOWNLOOPS_TOWN_SLUG
    bulk_endpoint = f"{base}/towns/{town_slug}/entries/bulk"
    batch_size = max(1, min(TOWNLOOPS_BULK_LIMIT, args.batch_size))

    only_kinds = None
    if args.only_kind:
        requested = {k.strip() for k in args.only_kind.split(",") if k.strip()}
        all_kinds = {"event", *ENTITY_KINDS}
        bad = requested - all_kinds
        if bad:
            print(f"[error] unknown kinds: {sorted(bad)}", file=sys.stderr)
            return 2
        only_kinds = requested

    per_kind: dict = {}
    overall_failures: list[dict] = []
    total_unpushed = total_shipped = total_failed = 0

    for kind in ["event", *ENTITY_KINDS]:
        if only_kinds and kind not in only_kinds:
            continue
        rows = _select_unpushed_by_kind(conn, kind)
        if not rows:
            per_kind[kind] = {"unpushed": 0, "new_posted": 0, "updates_patched": 0,
                              "shipped": 0, "failed": 0}
            continue
        new_rows = [r for r in rows if r["pushed_at"] is None]
        update_rows = [r for r in rows if r["pushed_at"] is not None]

        s_post, f_post, fails_post = _bulk_post(
            conn, kind, new_rows, bulk_endpoint, api_key, batch_size, args.dry_run,
        )
        s_patch = f_patch = 0
        fails_patch: list[dict] = []
        for r in update_rows:
            s, f, fs = _patch_one(conn, kind, r, base, town_slug, api_key, args.dry_run)
            s_patch += s
            f_patch += f
            fails_patch.extend(fs)

        per_kind[kind] = {
            "unpushed": len(rows),
            "new_posted": len(new_rows),
            "updates_patched": len(update_rows),
            "shipped": s_post + s_patch,
            "failed": f_post + f_patch,
        }
        total_unpushed += len(rows)
        total_shipped += s_post + s_patch
        total_failed += f_post + f_patch
        overall_failures.extend(fails_post + fails_patch)

    summary = {
        "per_kind": per_kind,
        "totals": {
            "unpushed": total_unpushed,
            "shipped": total_shipped,
            "failed": total_failed,
        },
        "dry_run": bool(args.dry_run),
    }
    if overall_failures:
        summary["failure_sample"] = overall_failures[:5]
    print(json.dumps(summary, separators=(",", ":")))
    return 0 if total_failed == 0 else 3


# =====================================================================
# Resync: reconcile local DB ship-state with Townloops' current state.
# Mostly cures the slug-drift problem — when Townloops rewrites a slug
# (admin edit, auto-rename on collision, etc.), our PATCH targets the
# wrong URL and 404s. Resync queries Townloops for each kind, matches
# rows to local by name + (date_day for events / name-only for entities),
# and writes the actual Townloops slug into the local row.
# =====================================================================


def _townloops_paginate(base: str, town: str, endpoint: str, api_key: str) -> list[dict]:
    """Pull every row of a kind from Townloops, paging at the API's max
    limit of 200 per request."""
    out: list[dict] = []
    offset = 0
    while True:
        url = f"{base}/towns/{town}/{endpoint}?limit=200&offset={offset}"
        r = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        r.raise_for_status()
        page = r.json().get("data") or []
        if not page:
            break
        out.extend(page)
        if len(page) < 200:
            break
        offset += 200
    return out


def _match_event_key(title: str, start_at: str) -> tuple:
    return (_norm(title), (start_at or "")[:10])


def _delete_townloops_row(base: str, town: str, kind: str, slug: str, api_key: str) -> int:
    """DELETE one row by slug. Returns HTTP status; 0 on network error."""
    url = _patch_endpoint_for_kind(base, town, kind, slug)
    try:
        r = requests.delete(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Idempotency-Key": f"hermes-{TOWNLOOPS_TOWN_SLUG}:resync-orphan:{kind}:{slug}",
            },
            timeout=15,
        )
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "30"))
            time.sleep(min(wait, 60))
            # one retry
            r = requests.delete(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Idempotency-Key": f"hermes-{TOWNLOOPS_TOWN_SLUG}:resync-orphan:{kind}:{slug}",
                },
                timeout=15,
            )
        return r.status_code
    except requests.RequestException:
        return 0


def _resync_kind(
    conn: sqlite3.Connection, kind: str, base: str, town: str, api_key: str,
    dry_run: bool, delete_orphans: bool = False,
) -> dict:
    """Pull Townloops state for one kind, match to local rows, write
    townloops_slug + reconcile pushed_at/pushed_hash. Returns counts."""
    endpoint = "events" if kind == "event" else ENTITY_REG[kind]["endpoint"]
    table = _ship_table_for_kind(kind)
    uid_col = "event_uid" if kind == "event" else "entity_uid"
    remote = _townloops_paginate(base, town, endpoint, api_key)
    # Build remote index. For events: (name_norm, date_day) → list of (slug, updated_at).
    # For entities: name_norm → list. Multiple matches mean Townloops has dupes;
    # we pick the newest updated_at.
    remote_idx: dict = {}
    for r in remote:
        if kind == "event":
            key = _match_event_key(r.get("title", ""), r.get("start_at", ""))
        else:
            key = (_norm(r.get("name", "")),)
        remote_idx.setdefault(key, []).append(r)
    # Fetch local rows that have hermes-shape uids (the ones we shipped).
    local_rows = list(conn.execute(f"SELECT * FROM {table}"))
    matched = unmatched = updated_slug = already_slugged = dupes_seen = 0
    orphans: list[dict] = []
    matched_remote_ids: set = set()
    for row in local_rows:
        if kind == "event":
            key = (_norm(row["name"]), row["date_day"])
        else:
            key = (_norm(row["name"]),)
        candidates = remote_idx.get(key, [])
        if not candidates:
            unmatched += 1
            continue
        # Pick the newest by updated_at if multiple
        chosen = max(candidates, key=lambda r: r.get("updated_at", ""))
        if len(candidates) > 1:
            dupes_seen += 1
        matched += 1
        matched_remote_ids.add(chosen.get("id"))
        existing_slug = None
        try:
            existing_slug = row["townloops_slug"]
        except (IndexError, KeyError):
            pass
        if existing_slug == chosen["slug"]:
            already_slugged += 1
            continue
        if dry_run:
            print(f"  [would-bind] {kind} '{row['name'][:50]}' -> {chosen['slug']}")
            updated_slug += 1
            continue
        with conn:
            conn.execute(
                f"UPDATE {table} SET townloops_slug = ?, "
                f"pushed_at = COALESCE(pushed_at, ?), pushed_hash = COALESCE(pushed_hash, content_hash) "
                f"WHERE {uid_col} = ?",
                (chosen["slug"], _iso_now(), row[uid_col]),
            )
        updated_slug += 1
    # Bucket Townloops rows that didn't get picked into two categories:
    # 1) Stale dupes — older versions of a row we DID bind to a local row.
    #    Safe to delete: we kept the fresher one, the older is garbage from
    #    a prior re-POST cycle. Includes both hermes-* and auto-slug forms.
    # 2) True orphans — no local row matched at all. Could be admin-curated
    #    seed data OR BBA-style mangled names we don't want. Reported only;
    #    operator decides per-row in admin.
    stale_dupes: list[dict] = []
    true_orphans: list[dict] = []
    matched_keys = set()
    for row in local_rows:
        if kind == "event":
            matched_keys.add((_norm(row["name"]), row["date_day"]))
        else:
            matched_keys.add((_norm(row["name"]),))
    for key, candidates in remote_idx.items():
        for c in candidates:
            if c.get("id") in matched_remote_ids:
                continue
            entry = {"slug": c.get("slug", ""), "name": c.get("title") or c.get("name", ""), "id": c.get("id")}
            if key in matched_keys:
                stale_dupes.append(entry)
            else:
                true_orphans.append(entry)

    # Delete-stale-dupes: prune the older-version Townloops rows that share
    # a name+date with a local row we've already bound to a different
    # (newer) Townloops slug. Hands-off the true-orphans bucket (might be
    # admin-curated content).
    deleted = delete_failed = 0
    if delete_orphans:
        for o in stale_dupes:
            if dry_run:
                print(f"  [would-delete-stale] {kind} '{o['name'][:50]}' slug={o['slug']}")
                deleted += 1
                continue
            status = _delete_townloops_row(base, town, kind, o["slug"], api_key)
            if status in (200, 204, 404):
                deleted += 1
            elif status in (401, 403):
                print(f"[fatal] DELETE {kind} {status} on {o['slug']}; aborting orphan cleanup", file=sys.stderr)
                raise SystemExit(1)
            else:
                delete_failed += 1
                print(f"[warn] DELETE {kind} {status} on {o['slug']}", file=sys.stderr)

    return {
        "remote_total": len(remote),
        "local_total": len(local_rows),
        "matched": matched,
        "already_slugged": already_slugged,
        "updated_slug": updated_slug,
        "unmatched_local": unmatched,
        "remote_with_local_dupes": dupes_seen,
        "stale_dupes_total": len(stale_dupes),
        "stale_dupes_deleted": deleted,
        "stale_dupes_delete_failed": delete_failed,
        "true_orphans": len(true_orphans),
        "true_orphan_sample": true_orphans[:5],
    }


def _cmd_resync(args) -> int:
    conn = connect()
    apply_migrations(conn)
    api_key = os.getenv("TOWNLOOPS_API_KEY", "").strip()
    if not api_key:
        print("[error] TOWNLOOPS_API_KEY not set in env", file=sys.stderr)
        return 2
    # TOWNLOOPS_DEFAULT_BASE already carries the /api/v1 segment; using the raw
    # env var here would drop it (the env var is always set, so the old `or`
    # short-circuit never reached the suffixed default). Matches townloops_api._url().
    base = TOWNLOOPS_DEFAULT_BASE.rstrip("/")
    town_slug = args.town or TOWNLOOPS_TOWN_SLUG
    only_kinds = None
    if args.only_kind:
        requested = {k.strip() for k in args.only_kind.split(",") if k.strip()}
        all_kinds = {"event", *ENTITY_KINDS}
        bad = requested - all_kinds
        if bad:
            print(f"[error] unknown kinds: {sorted(bad)}", file=sys.stderr)
            return 2
        only_kinds = requested
    per_kind: dict = {}
    for kind in ["event", *ENTITY_KINDS]:
        if only_kinds and kind not in only_kinds:
            continue
        print(f"--- resync {kind} ---", file=sys.stderr)
        per_kind[kind] = _resync_kind(
            conn, kind, base, town_slug, api_key, args.dry_run,
            delete_orphans=bool(getattr(args, "delete_orphans", False)),
        )
    print(json.dumps({
        "per_kind": per_kind,
        "dry_run": bool(args.dry_run),
        "delete_orphans": bool(getattr(args, "delete_orphans", False)),
    }, indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="events.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="one-shot fetch + aggregate, JSON to stdout (no DB writes)")
    f.add_argument("--since", help="ISO date/datetime; drop earlier events (default: now)")
    f.add_argument("--until", help="ISO date/datetime; drop later events (default: no cap)")
    f.add_argument("--sources", default=str(SOURCES_PATH))
    f.add_argument("--only", help="comma-separated source names to include")
    f.add_argument("--out", help="write JSON to file (default: stdout)")
    f.add_argument("--compact", action="store_true")
    f.set_defaults(func=_cmd_fetch)

    ls = sub.add_parser("list-sources", help="list configured sources")
    ls.add_argument("--sources", default=str(SOURCES_PATH))
    ls.set_defaults(func=_cmd_list_sources)

    v = sub.add_parser("validate", help="hit every source, report counts")
    v.add_argument("--sources", default=str(SOURCES_PATH))
    v.add_argument("--since", help="ISO date/datetime (default: now)")
    v.set_defaults(func=_cmd_validate)

    ing = sub.add_parser("ingest", help="fetch + upsert into data.db, log source_runs")
    ing.add_argument("--sources", default=str(SOURCES_PATH))
    ing.add_argument("--only", help="comma-separated source names to include")
    ing.add_argument("--dry-run", action="store_true", help="fetch but don't write to DB")
    ing.set_defaults(func=_cmd_ingest)

    am = sub.add_parser("add-manual", help="insert/update one manual event")
    am.add_argument("--slug", help="slug; becomes source='manual:<slug>'")
    am.add_argument("--name")
    am.add_argument("--date", help="ISO date or datetime")
    am.add_argument("--location")
    am.add_argument("--venue-name", dest="venue_name")
    am.add_argument("--end-at", dest="end_at", help="ISO datetime")
    am.add_argument("--description")
    am.add_argument("--hosting-group", dest="hosting_group")
    am.add_argument("--source-url", dest="source_url")
    am.add_argument("--category-slug", dest="category_slug",
                    help="Townloops category slug (e.g. live-music, outdoors, community)")
    am.add_argument("--from-stdin-json", dest="from_stdin_json", action="store_true",
                    help="read a single JSON object from stdin")
    am.set_defaults(func=_cmd_add_manual)

    im = sub.add_parser("import-manual", help="bulk-import manual events from YAML or JSON")
    im.add_argument("file")
    im.set_defaults(func=_cmd_import_manual)

    ll = sub.add_parser("list", help="query data.db")
    ll.add_argument("--kind", default="event", choices=["event", *ENTITY_KINDS],
                    help="entity kind to list (default: event)")
    ll.add_argument("--since", help="ISO date/datetime (events only)")
    ll.add_argument("--until", help="ISO date/datetime (events only)")
    ll.add_argument("--source")
    ll.add_argument("--source-kind", choices=["scraped", "manual", "mined"], dest="source_kind")
    ll.add_argument("--unpushed", action="store_true")
    ll.add_argument("--limit", type=int)
    ll.add_argument("--table", action="store_true", help="print as columns instead of JSON")
    ll.set_defaults(func=_cmd_list)

    fl = sub.add_parser("flush", help="write unpushed rows of one kind to data/outbox/<batch>.json")
    fl.add_argument("--kind", default="event", choices=["event", *ENTITY_KINDS],
                    help="entity kind to flush (default: event)")
    fl.add_argument("--batch-size", type=int, default=100, dest="batch_size")
    fl.add_argument("--dry-run", action="store_true", help="print payload, don't write or mark pushed")
    fl.set_defaults(func=_cmd_flush)

    sh = sub.add_parser("ship", help="POST unpushed events + entities to Townloops")
    sh.add_argument("--batch-size", type=int, default=100, dest="batch_size",
                    help="entries per request (server cap 100)")
    sh.add_argument("--town", default=TOWNLOOPS_TOWN_SLUG, help="town_slug (default from TOWNLOOPS_TOWN_SLUG env)")
    sh.add_argument("--only-kind", dest="only_kind",
                    help="comma-separated kinds to ship (e.g. 'venue,performer'); default: all")
    sh.add_argument("--dry-run", action="store_true",
                    help="print payload, don't POST or mark pushed")
    sh.set_defaults(func=_cmd_ship)

    kk = sub.add_parser("kinds", help="show per-kind row counts and unpushed counts")
    kk.set_defaults(func=_cmd_kinds)

    rs = sub.add_parser("resync", help="reconcile local townloops_slug with Townloops state (cures slug drift)")
    rs.add_argument("--town", default=TOWNLOOPS_TOWN_SLUG, help="town_slug (default from TOWNLOOPS_TOWN_SLUG env)")
    rs.add_argument("--only-kind", dest="only_kind",
                    help="comma-separated kinds to resync (default: all)")
    rs.add_argument("--dry-run", action="store_true",
                    help="print what would change without writing")
    rs.add_argument("--delete-orphans", action="store_true", dest="delete_orphans",
                    help="DELETE Townloops rows whose slug starts with 'hermes-' "
                         "AND don't match any local row (i.e. our stale shipping "
                         "dupes from prior slug-drift cycles). Never touches "
                         "seed/admin-curated rows. Requires a key with delete perm.")
    rs.set_defaults(func=_cmd_resync)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
