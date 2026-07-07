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
import unicodedata
import uuid
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal
from urllib.parse import urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from dateutil import tz
from icalendar import Calendar

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

# Timezone (per-town override via AGENT_TZ).
PT = tz.gettz(os.environ.get("AGENT_TZ", "America/Los_Angeles"))

FIRECRAWL_CLOUD_BASE = "https://api.firecrawl.dev"
DEFAULT_MAX_DETAIL_FETCHES = 50

TOWNLOOPS_DEFAULT_BASE = os.environ.get(
    "TOWNLOOPS_API_URL", "http://localhost:8004"
).rstrip("/") + "/api/v1"


def _require_town_slug() -> str:
    slug = os.environ.get("TOWNLOOPS_TOWN_SLUG")
    if not slug:
        sys.stderr.write(
            "FATAL: TOWNLOOPS_TOWN_SLUG env-var is required. "
            "Set it in .env before running any ingest command.\n"
        )
        raise SystemExit(2)
    return slug


TOWNLOOPS_TOWN_SLUG = _require_town_slug()
TOWNLOOPS_BULK_LIMIT = 100


def _firecrawl_endpoint() -> str:
    """Honor FIRECRAWL_API_URL (self-hosted) if set, else fall back to the cloud."""
    base = (os.getenv("FIRECRAWL_API_URL") or FIRECRAWL_CLOUD_BASE).rstrip("/")
    return f"{base}/v1/scrape"


@dataclass
class Event:
    name: str
    location: str
    date: str
    description: str
    hosting_group: str
    source: str = ""
    source_url: str = ""
    # Added 2026-06-24 for the Townloops EventPayload mapping (venue_name,
    # end_at, category_slug). Parsers fill them when source data permits;
    # otherwise empty string and they're omitted from the API payload.
    venue_name: str = ""
    end_at: str = ""
    category_slug: str = ""
    # In-memory only — NOT persisted to DB, NOT in content_hash, NOT shipped
    # to Townloops. Parsers stash raw structured fields here (JSON-LD
    # organizer/performer/creator, iCal ORGANIZER) so _mine_entities() can
    # build entity rows in the same ingest pass. Excluded from asdict() via
    # explicit handling in _row_to_payload (it's not a row attribute anyway).
    mining_extras: dict = field(default_factory=dict)

    def dedupe_key(self) -> tuple:
        return (
            self.name.strip().lower(),
            (self.date or "")[:10],
            self.location.strip().lower()[:40],
        )


# =====================================================================
# Entity types — Townloops Phase B/C payload mirrors.
#
# Each dataclass matches a Townloops payload contract. Mining produces
# these from already-parsed events (see _mine_entities). Shared audit
# fields (source/source_url/extra) live on every dataclass so
# _upsert_entity can be generic over the kind.
# =====================================================================


@dataclass
class Venue:
    name: str
    description: str = ""
    address: str = ""
    website: str = ""
    phone: str = ""
    accessibility_notes: str = ""
    image_path: str = ""
    image_alt: str = ""
    category_slug: str = ""
    source: str = ""
    source_url: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Business:
    name: str
    description: str = ""
    address: str = ""
    website: str = ""
    phone: str = ""
    category_slug: str = ""
    source: str = ""
    source_url: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Community:
    name: str
    kind: str = "group"
    description: str = ""
    website: str = ""
    meeting_info: str = ""
    category_slug: str = ""
    source: str = ""
    source_url: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Performer:
    name: str
    kind: str = "band"
    description: str = ""
    website: str = ""
    instagram: str = ""
    spotify_url: str = ""
    image_path: str = ""
    image_alt: str = ""
    source: str = ""
    source_url: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Creator:
    name: str
    kind: str = "other"
    bio: str = ""
    website: str = ""
    instagram: str = ""
    youtube_url: str = ""
    image_path: str = ""
    image_alt: str = ""
    source: str = ""
    source_url: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class MinedEntity:
    kind: str          # "venue" | "business" | "community" | "performer" | "creator"
    data: object       # one of the five dataclasses above


# ENTITY_REG drives _upsert_entity, _select_unpushed(kind), and ship URL
# construction. `endpoint` is the URL path segment in Townloops (avoids
# the `business`+`s` -> `businesss` bug and `community`+`s` -> `communitys`).
# `cols` is the explicit ordered list of DB columns to read/write.
# `hash_cols` is the subset of `cols` that participate in content_hash
# (excludes source / source_url / extra_json since those don't represent
# the entity's identity in Townloops).
ENTITY_REG: dict = {
    "venue": {
        "table": "venues",
        "endpoint": "venues",
        "cls": Venue,
        "cols": ["name", "description", "address", "website", "phone",
                 "accessibility_notes", "image_path", "image_alt", "category_slug"],
    },
    "business": {
        "table": "businesses",
        "endpoint": "businesses",
        "cls": Business,
        "cols": ["name", "description", "address", "website", "phone", "category_slug"],
    },
    "community": {
        "table": "communities",
        "endpoint": "communities",
        "cls": Community,
        "cols": ["name", "kind", "description", "website", "meeting_info", "category_slug"],
    },
    "performer": {
        "table": "performers",
        "endpoint": "performers",
        "cls": Performer,
        "cols": ["name", "kind", "description", "website", "instagram", "spotify_url",
                 "image_path", "image_alt"],
    },
    "creator": {
        "table": "creators",
        "endpoint": "creators",
        "cls": Creator,
        "cols": ["name", "kind", "bio", "website", "instagram", "youtube_url",
                 "image_path", "image_alt"],
    },
}
# All payload cols participate in content_hash.
for _spec in ENTITY_REG.values():
    _spec["hash_cols"] = list(_spec["cols"])
ENTITY_KINDS = list(ENTITY_REG.keys())


def _now_pt() -> datetime:
    return datetime.now(tz=PT)


def _iso_now() -> str:
    return _now_pt().isoformat()


def _parse_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        d = value
    else:
        s = str(value)
        try:
            d = dateparser.parse(s)
        except (ValueError, TypeError, OverflowError):
            # Strict parse failed — many real-world strings include
            # decorative tokens (e.g. "July 6, 2026 @ 7:30 p.m.") that
            # `fuzzy=True` tolerates by ignoring the unknown tokens.
            try:
                d = dateparser.parse(s, fuzzy=True)
            except (ValueError, TypeError, OverflowError):
                return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=PT)
    return d


def _iso(d: datetime) -> str:
    return d.isoformat()


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s, n: int = 500) -> str:
    """Decode HTML entities (twice if doubly-escaped), strip tags, collapse whitespace, truncate."""
    if not s:
        return ""
    text = str(s)
    text = html.unescape(text)
    if "&" in text and ";" in text:
        text = html.unescape(text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = " ".join(text.split())
    if len(text) > n:
        text = text[: n - 1] + "…"
    return text


def _truncate(s, n: int = 500) -> str:
    return _clean(s, n)


def _norm(s: str) -> str:
    """Unicode-normalize, lowercase, collapse whitespace. For uid hashing."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    return " ".join(s.lower().strip().split())


def _venue_from_location(location: str) -> str:
    """Take the first comma-segment as the venue name guess.

    Examples:
      'Lakeview Commons, South Lake Tahoe, CA' -> 'Lakeview Commons'
      'TRPA Offices, 128 Market Street, Stateline, NV' -> 'TRPA Offices'
      '1000 Rufus Allen Blvd, South Lake Tahoe' -> '1000 Rufus Allen Blvd'
        (an address — Townloops will warn venue_not_found, event still saves)

    Empty or no comma → return the input stripped (could be a single venue name).
    """
    if not location:
        return ""
    head = location.split(",", 1)[0].strip()
    return head[:120]


# Regex pair for extracting a venue name from event description text. Used as
# a fallback when the structured location field is just a town (common with
# aggregator sources like South Tahoe Now whose JSON-LD location.name is
# "South Lake Tahoe, CA" because they aggregate from many real venues).
#
# Pattern 1 ("at X in <town>") catches the most common phrasing:
#   "The event will take place at Riva Grill in South Lake Tahoe"
# Pattern 2 ("at X[,.]") catches the comma/period-terminated variant.
# Both require the venue to start with a capital letter — addresses
# ("at 1000 Rufus Allen Blvd...") fail the regex by design (Townloops venue
# lookup is exact-name match; a street address would never bind).
_VENUE_FROM_DESC_RE_1 = re.compile(
    r'\bat\s+(?:the\s+)?'
    r'([A-Z][A-Za-z0-9&\'\-]+(?:\s+[A-Z][A-Za-z0-9&\'\-]+){0,4})'
    r'\s+in\b'
)
_VENUE_FROM_DESC_RE_2 = re.compile(
    r'\bat\s+(?:the\s+)?'
    r'([A-Z][A-Za-z0-9&\'\-]+(?:\s+[A-Z][A-Za-z0-9&\'\-]+){0,3})'
    r'\s*[,\.]'
)


def _extract_venue_from_description(description: str) -> str:
    """Heuristic: pull a venue name from common phrasings in event descriptions.

    Returns "" if no confident match. Conservative: only matches Title-Case
    words after 'at' that resemble proper-noun venue names.
    """
    if not description:
        return ""
    m = _VENUE_FROM_DESC_RE_1.search(description)
    if not m:
        m = _VENUE_FROM_DESC_RE_2.search(description)
    if m:
        return m.group(1).strip()[:120]
    return ""


def _should_replace_venue(venue_name: str, source: dict) -> bool:
    """True when the parser-extracted venue is generic (a town or skip-listed)
    and we should try harder to find a real venue via description heuristics.

    Compares against both the full skip-list/default_location form AND the
    first-comma-stripped form, since `_venue_from_location` strips ", CA"
    suffixes from the venue_name before we get here — so a venue derived
    from "South Lake Tahoe, CA" arrives as "South Lake Tahoe".
    """
    if not venue_name:
        return True
    vn_norm = _norm(venue_name)
    default_loc = source.get("default_location", "")
    placeholders = {_norm(default_loc), _norm(_venue_from_location(default_loc))}
    for n in (source.get("skip_venue_names") or []):
        placeholders.add(_norm(n))
        placeholders.add(_norm(_venue_from_location(n)))
    placeholders.discard("")
    return vn_norm in placeholders


def _http_get(url: str, timeout: int = 20) -> requests.Response:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; HermesEventAggregator/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml,text/calendar,application/rss+xml,*/*",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------


def parse_ical(source: dict) -> list[Event]:
    r = _http_get(source["url"])
    cal = Calendar.from_ical(r.content)
    out: list[Event] = []
    for comp in cal.walk("VEVENT"):
        dt = comp.get("dtstart")
        if dt is None:
            continue
        dt_val = dt.dt
        if isinstance(dt_val, datetime):
            if dt_val.tzinfo is None:
                dt_val = dt_val.replace(tzinfo=PT)
        else:
            dt_val = datetime.combine(dt_val, datetime.min.time()).replace(tzinfo=PT)
        end_iso = ""
        dtend = comp.get("dtend")
        if dtend is not None:
            ev = dtend.dt
            if isinstance(ev, datetime):
                if ev.tzinfo is None:
                    ev = ev.replace(tzinfo=PT)
            else:
                ev = datetime.combine(ev, datetime.min.time()).replace(tzinfo=PT)
            end_iso = _iso(ev)
        location = _clean(comp.get("location", ""), 200) or source.get("default_location", "")

        # Mining extras: iCal ORGANIZER often carries CN= (common name) param.
        mx: dict = {}
        org = comp.get("organizer")
        if org is not None:
            try:
                params = getattr(org, "params", {}) or {}
                cn = params.get("CN") or params.get("cn") or ""
                if cn:
                    mx["organizer"] = {"name": _clean(str(cn), 200), "url": "", "type": "Organization"}
                else:
                    s = str(org)
                    # strip mailto: prefix; if it's an email, drop the domain part
                    s = s.replace("mailto:", "").strip()
                    if s and "@" not in s:
                        mx["organizer"] = {"name": _clean(s, 200), "url": "", "type": "Organization"}
            except Exception:
                pass

        venue_guess = _venue_from_location(location)
        desc_clean = _clean(comp.get("description", ""))
        if _should_replace_venue(venue_guess, source):
            better = _extract_venue_from_description(desc_clean)
            if better:
                venue_guess = better
        out.append(
            Event(
                name=_clean(comp.get("summary", ""), 200),
                location=location,
                venue_name=venue_guess,
                date=_iso(dt_val),
                end_at=end_iso,
                description=desc_clean,
                hosting_group=source.get("hosting_group", source["name"]),
                source=source["name"],
                source_url=str(comp.get("url", "")) or source["url"],
                category_slug=source.get("default_category_slug", ""),
                mining_extras=mx,
            )
        )
    return out


def _walk_jsonld(blob) -> Iterable[dict]:
    if isinstance(blob, dict):
        yield blob
        if isinstance(blob.get("@graph"), list):
            for item in blob["@graph"]:
                yield from _walk_jsonld(item)
        for v in blob.values():
            if isinstance(v, (list, dict)):
                yield from _walk_jsonld(v)
    elif isinstance(blob, list):
        for item in blob:
            yield from _walk_jsonld(item)


def _jsonld_to_event(item: dict, source: dict, page_url: str) -> Event | None:
    name = item.get("name") or ""
    start = item.get("startDate") or item.get("startTime")
    if not name or not start:
        return None
    loc = item.get("location") or {}
    location = ""
    venue_name = ""
    if isinstance(loc, dict):
        venue_name = (loc.get("name") or "").strip()
        parts = [venue_name or None]
        addr = loc.get("address")
        if isinstance(addr, dict):
            parts += [
                addr.get("streetAddress"),
                addr.get("addressLocality"),
                addr.get("addressRegion"),
            ]
        elif isinstance(addr, str):
            parts.append(addr)
        location = ", ".join(p for p in parts if p)
    elif isinstance(loc, list) and loc:
        first = loc[0]
        if isinstance(first, dict):
            venue_name = (first.get("name") or "").strip()
            location = venue_name
        elif isinstance(first, str):
            location = first
    elif isinstance(loc, str):
        location = loc
    organizer = item.get("organizer") or {}
    organizer_extra = None
    if isinstance(organizer, dict):
        host = organizer.get("name") or ""
        if host:
            organizer_extra = {"name": _clean(host, 200), "url": organizer.get("url") or organizer.get("@id") or "", "type": organizer.get("@type") or ""}
    elif isinstance(organizer, list) and organizer:
        first = organizer[0] or {}
        host = first.get("name", "") if isinstance(first, dict) else ""
        if host:
            organizer_extra = {"name": _clean(host, 200), "url": first.get("url", "") if isinstance(first, dict) else "", "type": first.get("@type", "") if isinstance(first, dict) else ""}
    else:
        host = str(organizer) if organizer else ""
        if host:
            organizer_extra = {"name": _clean(host, 200), "url": "", "type": ""}
    host = host or source.get("hosting_group", source["name"])

    # Mining extras: raw JSON-LD performer/creator/organizer for _mine_entities.
    # NOT persisted to events table, NOT shipped to Townloops.
    mx: dict = {}
    if organizer_extra:
        mx["organizer"] = organizer_extra

    def _people_extras(blob) -> list:
        out = []
        if isinstance(blob, dict):
            items = [blob]
        elif isinstance(blob, list):
            items = blob
        else:
            return out
        for p in items:
            if not isinstance(p, dict):
                continue
            nm = (p.get("name") or "").strip()
            if not nm:
                continue
            out.append({
                "name": _clean(nm, 200),
                "url": p.get("url") or p.get("sameAs") or "",
                "type": p.get("@type") or "",
            })
        return out

    perfs = _people_extras(item.get("performer"))
    crts = _people_extras(item.get("creator"))
    if perfs:
        mx["performers"] = perfs
    if crts:
        mx["creators"] = crts

    location_final = _clean(location, 200) or source.get("default_location", "")
    venue_final = _clean(venue_name, 120) or _venue_from_location(location_final)
    # Aggregators (South Tahoe Now, Visit Lake Tahoe) often only put the town
    # in location/venue_name. When the venue we've got is just the town,
    # try to mine the real venue from the description text.
    if _should_replace_venue(venue_final, source):
        better = _extract_venue_from_description(_clean(item.get("description") or "", 2000))
        if better:
            venue_final = better
    return Event(
        name=_clean(name, 200),
        location=location_final,
        venue_name=venue_final,
        date=str(start),
        end_at=str(item.get("endDate") or item.get("endTime") or ""),
        description=_clean(item.get("description")),
        hosting_group=_clean(host, 200) or source.get("hosting_group", source["name"]),
        source=source["name"],
        source_url=str(item.get("url") or page_url),
        category_slug=source.get("default_category_slug", ""),
        mining_extras=mx,
    )


def _extract_jsonld_from_html(html_text: str, source: dict, page_url: str) -> list[Event]:
    soup = BeautifulSoup(html_text, "lxml")
    out: list[Event] = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            blob = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk_jsonld(blob):
            t = item.get("@type")
            if t == "Event" or (isinstance(t, list) and "Event" in t):
                ev = _jsonld_to_event(item, source, page_url)
                if ev:
                    out.append(ev)
    return out


def parse_jsonld(source: dict) -> list[Event]:
    r = _http_get(source["url"])
    return _extract_jsonld_from_html(r.text, source, source["url"])


def parse_rss(source: dict) -> list[Event]:
    feed = feedparser.parse(source["url"])
    out: list[Event] = []
    default_location = source.get("default_location", "")
    for entry in feed.entries:
        when = entry.get("published") or entry.get("updated")
        out.append(
            Event(
                name=_clean(entry.get("title", ""), 200),
                location=default_location,
                venue_name=_venue_from_location(default_location),
                date=str(when or ""),
                description=_clean(entry.get("summary", "")),
                hosting_group=source.get("hosting_group", source["name"]),
                source=source["name"],
                source_url=entry.get("link", source["url"]),
                category_slug=source.get("default_category_slug", ""),
            )
        )
    return out


def _text(node, selector):
    if not selector:
        return ""
    el = node.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def _attr_or_text(node, selector, attr):
    if not selector:
        return ""
    el = node.select_one(selector)
    if not el:
        return ""
    if attr:
        return el.get(attr, "") or ""
    return el.get_text(" ", strip=True)


def _extract_selectors_from_html(html_text: str, source: dict, page_url: str) -> list[Event]:
    soup = BeautifulSoup(html_text, "lxml")
    sel = source.get("selectors") or {}
    container = sel.get("container")
    if not container:
        return []

    # Optional: pull date from container text via regex when there's no clean
    # CSS hook for it. Group 1 (or full match if no groups) is fed to dateutil.
    # If `date_regex_all` is true, emit one Event per regex match in the
    # container — useful when one block lists multiple recurring dates.
    date_re = re.compile(sel["date_regex"]) if sel.get("date_regex") else None
    date_re_all = bool(sel.get("date_regex_all"))
    end_re = re.compile(sel["end_date_regex"]) if sel.get("end_date_regex") else None

    name_prefix = sel.get("name_prefix") or ""
    # name_exclude: list of exact names (case-insensitive) to skip. Useful when
    # the container selector also matches non-event blocks like section headers.
    name_exclude = {n.strip().lower() for n in (sel.get("name_exclude") or [])}
    out: list[Event] = []
    for node in soup.select(container):
        name = _text(node, sel.get("name"))
        if not name:
            continue
        if name.strip().lower() in name_exclude:
            continue
        if name_prefix:
            name = f"{name_prefix}{name}"
        loc = _text(node, sel.get("location")) or source.get("default_location", "")
        venue_explicit = _text(node, sel.get("venue_name")) if sel.get("venue_name") else ""
        venue_name = _clean(venue_explicit, 120) or _venue_from_location(loc)
        venue_override = (source.get("venue_name_overrides") or {}).get(venue_name)
        if venue_override:
            venue_name = _clean(str(venue_override), 120)
        desc = _truncate(_text(node, sel.get("description")))
        link = _attr_or_text(node, sel.get("link"), sel.get("link_attr") or "href")
        if link and link.startswith("/"):
            link = urljoin(page_url, link)
        # Optional separate time-of-day selector; concatenated to each date.
        time_str = _text(node, sel.get("time")) if sel.get("time") else ""

        if date_re:
            node_text = node.get_text(" ", strip=True)
            matches = list(date_re.finditer(node_text))
            if not matches:
                continue
            picked = matches if date_re_all else matches[:1]
            date_strs = [(m.group(1) if m.groups() else m.group(0)) for m in picked]
        else:
            ds = _attr_or_text(node, sel.get("date"), sel.get("date_attr"))
            if not ds:
                continue
            date_strs = [ds]
        if time_str:
            date_strs = [f"{ds} {time_str}" for ds in date_strs]

        if end_re:
            em = end_re.search(node.get_text(" ", strip=True))
            end_str_default = (em.group(1) if (em and em.groups()) else (em.group(0) if em else ""))
        else:
            end_str_default = _attr_or_text(node, sel.get("end_date"), sel.get("end_date_attr"))

        for ds in date_strs:
            out.append(
                Event(
                    name=_clean(name, 200),
                    location=_clean(loc, 200),
                    venue_name=venue_name,
                    date=ds,
                    end_at=end_str_default,
                    description=desc,
                    hosting_group=source.get("hosting_group", source["name"]),
                    source=source["name"],
                    source_url=link or page_url,
                    category_slug=source.get("default_category_slug", ""),
                )
            )
    return out


def parse_selectors(source: dict) -> list[Event]:
    r = _http_get(source["url"])
    return _extract_selectors_from_html(r.text, source, source["url"])


def _firecrawl_scrape(url: str, timeout: int = 60) -> str:
    """POST to Firecrawl's /v1/scrape and return rendered HTML.

    Honors FIRECRAWL_API_URL for self-hosted instances. Falls back to the
    cloud endpoint. Self-hosted instances ignore the bearer token but the
    Firecrawl API still requires one to be present.
    """
    endpoint = _firecrawl_endpoint()
    is_self_hosted = bool(os.getenv("FIRECRAWL_API_URL"))
    key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if not key and not is_self_hosted:
        raise RuntimeError(
            "FIRECRAWL_API_KEY not set and no FIRECRAWL_API_URL configured. "
            "Either set FIRECRAWL_API_KEY for cloud, or FIRECRAWL_API_URL for self-host."
        )
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    elif is_self_hosted:
        headers["Authorization"] = "Bearer self-hosted"
    resp = requests.post(
        endpoint,
        headers=headers,
        json={"url": url, "formats": ["html"]},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data") or {}
    rendered = data.get("html") or data.get("rawHtml") or ""
    if not rendered:
        raise RuntimeError(f"Firecrawl returned no html for {url}: {body!r}"[:500])
    return rendered


def _detail_links_from_html(html_text: str, base_url: str, selector: str, cap: int) -> list[str]:
    soup = BeautifulSoup(html_text, "lxml")
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.select(selector):
        href = a.get("href")
        if not href:
            continue
        full = urljoin(base_url, href)
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
        if len(out) >= cap:
            break
    return out


def parse_web(source: dict) -> list[Event]:
    """JS-tier parser. Fetches rendered HTML via Firecrawl, then extracts.

    Modes (set via `web_extract`):
      - jsonld: walk JSON-LD Event blobs in the rendered HTML
      - selectors: apply the `selectors` block to the rendered HTML

    Optional `crawl_detail_links: true` + `detail_link_selector` makes the
    listing page only a source of links — each linked detail page is then
    scraped and extracted individually. Cap at `max_detail_fetches`
    (default 50) to bound Firecrawl spend.
    """
    mode = source.get("web_extract", "jsonld")
    if mode not in ("jsonld", "selectors"):
        raise ValueError(f"web_extract must be 'jsonld' or 'selectors' (got {mode!r})")
    page_html = _firecrawl_scrape(source["url"])

    def extract(html_text: str, page_url: str) -> list[Event]:
        if mode == "jsonld":
            return _extract_jsonld_from_html(html_text, source, page_url)
        return _extract_selectors_from_html(html_text, source, page_url)

    if not source.get("crawl_detail_links"):
        return extract(page_html, source["url"])

    sel = source.get("detail_link_selector")
    if not sel:
        raise ValueError(
            f"{source['name']}: crawl_detail_links=true requires detail_link_selector"
        )
    cap = int(source.get("max_detail_fetches", DEFAULT_MAX_DETAIL_FETCHES))
    links = _detail_links_from_html(page_html, source["url"], sel, cap)
    if len(links) >= cap:
        print(
            f"[warn] {source['name']}: detail-link cap of {cap} hit; "
            "raise max_detail_fetches in sources.yaml if you want more",
            file=sys.stderr,
        )
    out: list[Event] = []
    for url in links:
        try:
            sub_html = _firecrawl_scrape(url)
        except Exception as exc:
            print(f"[warn] {source['name']}: detail scrape {url!r}: {exc}", file=sys.stderr)
            continue
        out.extend(extract(sub_html, url))
    return out


PARSERS = {
    "ical": parse_ical,
    "jsonld": parse_jsonld,
    "rss": parse_rss,
    "selectors": parse_selectors,
    "web": parse_web,
}


# ---------------------------------------------------------------------
# Enrichment: per-source `enrichment_urls` overlay additional fields onto
# events whose name matches. Use when a primary listing source has minimal
# detail (e.g. JSON-LD with just title/date) and per-event detail pages
# carry the synopsis / accessibility notes / etc.
#
# sources.yaml shape:
#
#   enrichment_urls:
#     - url: https://example.com/play/macbeth/
#       matches_name: Macbeth               # case-insensitive exact match on Event.name
#       fetch: requests                     # 'requests' (default) or 'web' (Firecrawl render)
#       overwrite: true                     # true (default) = enriched value wins; false = only fill empties
#       selectors:
#         description: ".some-css"          # short form: take first non-empty match
#         accessibility_notes:              # long form: filter by text length
#           css: ".elementor-widget-text-editor"
#           min_length: 50
#           max_length: 800
#           nth: 1                          # optional: pick the Nth match within the length filter (0-indexed)
# ---------------------------------------------------------------------


def _extract_enrichment_field(soup, spec) -> str:
    """Return text matched by `spec` from `soup`, or empty string.

    `spec` is either:
      - string: CSS selector; return first match's stripped text
      - dict: {css, min_length?, max_length?, nth?} for length-filtered selection
    """
    if isinstance(spec, str):
        el = soup.select_one(spec)
        return el.get_text(" ", strip=True) if el else ""
    if not isinstance(spec, dict):
        return ""
    css = spec.get("css")
    if not css:
        return ""
    mn = int(spec.get("min_length", 0))
    mx = int(spec.get("max_length", 100000))
    nth = int(spec.get("nth", 0))
    candidates = []
    for el in soup.select(css):
        t = el.get_text(" ", strip=True)
        if mn <= len(t) <= mx:
            candidates.append(t)
    if nth < len(candidates):
        return candidates[nth]
    return ""


def _fetch_for_enrichment(url: str, mode: str) -> str:
    if mode == "web":
        return _firecrawl_scrape(url)
    return _http_get(url).text


def _apply_enrichments(events: list[Event], source: dict) -> None:
    """In-place: overlay extra fields onto events whose name matches an enrichment entry."""
    enrichments = source.get("enrichment_urls") or []
    if not enrichments or not events:
        return
    for enr in enrichments:
        url = enr.get("url")
        match_name = (enr.get("matches_name") or "").strip().lower()
        sel_specs = enr.get("selectors") or {}
        if not (url and match_name and sel_specs):
            continue
        try:
            html_text = _fetch_for_enrichment(url, enr.get("fetch", "requests"))
        except Exception as exc:
            print(f"[warn] enrichment {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        soup = BeautifulSoup(html_text, "lxml")
        extracted = {}
        for field, spec in sel_specs.items():
            value = _extract_enrichment_field(soup, spec)
            if value:
                # Conservative truncation: descriptions go up to 1500 chars,
                # other fields keep their natural length.
                limit = 1500 if field == "description" else 1000
                extracted[field] = _clean(value, limit)
        if not extracted:
            print(f"[warn] enrichment {url}: matched 0 selectors", file=sys.stderr)
            continue
        overwrite = enr.get("overwrite", True)
        applied = 0
        for ev in events:
            if ev.name.strip().lower() != match_name:
                continue
            for field, value in extracted.items():
                cur = getattr(ev, field, None)
                if overwrite or not cur:
                    setattr(ev, field, value)
            applied += 1
        print(f"[enrich] {url} → {applied} events ({', '.join(extracted)})", file=sys.stderr)


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
        parser_name = src.get("parser")
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


def _event_uid(ev: Event) -> str:
    parts = [
        ev.source or "",
        _norm(ev.name),
        (ev.date or "")[:10],
        _norm(ev.location)[:40],
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _content_hash(ev: Event, extra: dict | None = None) -> str:
    payload = {
        "source": ev.source,
        "source_url": ev.source_url,
        "name": ev.name,
        "hosting_group": ev.hosting_group,
        "location": ev.location,
        "venue_name": ev.venue_name,
        "date": ev.date,
        "end_at": ev.end_at,
        "description": ev.description,
        "category_slug": ev.category_slug,
        "extra": extra or {},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


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


def _entity_uid(kind: str, name: str) -> str:
    """Stable per-(kind, name) identity. Cross-source collapse by design."""
    parts = [kind, _norm(name)]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _entity_content_hash(kind: str, ent) -> str:
    """sha1 over the kind's payload fields. Drives PATCH-vs-no-op detection."""
    spec = ENTITY_REG[kind]
    payload = {col: getattr(ent, col, "") or "" for col in spec["hash_cols"]}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


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

    `slug` = deterministic 'hermes-<event_uid[:16]>' so re-ships hit
    `409 slug_taken`, which the ship loop treats as 'already shipped'.
    """
    payload = _row_to_event_payload(row)
    payload["slug"] = f"hermes-{row['event_uid'][:16]}"
    return {
        "entity_type": "event",
        "payload": payload,
        "metadata": {
            "source": row["source"],
            "source_kind": row["source_kind"],
            "hermes_event_uid": row["event_uid"],
        },
    }


def _townloops_slug_for(row: sqlite3.Row) -> str:
    return f"hermes-{row['event_uid'][:16]}"


# =====================================================================
# Kind-generic ship helpers.
# =====================================================================


def _row_uid(row: sqlite3.Row, kind: str) -> str:
    return row["event_uid"] if kind == "event" else row["entity_uid"]


def _slug_for_kind(row: sqlite3.Row, kind: str) -> str:
    """Default outgoing slug — used as `slug:` on first POST. Townloops may
    accept it (preserving our hermes-<hash> form) or rewrite to its own
    auto-format on collision."""
    return f"hermes-{_row_uid(row, kind)[:16]}"


def _patch_target_slug(row: sqlite3.Row, kind: str) -> str:
    """Slug to use in the PATCH URL: prefer the actual Townloops slug
    captured at POST/PATCH time, fall back to the deterministic form for
    rows that predate the slug-capture feature."""
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
    payload["slug"] = _slug_for_kind(row, kind)
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
        try:
            resp = requests.post(
                bulk_endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": f"hermes-slt:{kind}:post:{uuid.uuid4().hex}",
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
                "Idempotency-Key": f"hermes-slt:{kind}:patch:{uuid.uuid4().hex}",
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
            "agent": "hermes/south-lake-tahoe-events",
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
    base = (os.getenv("TOWNLOOPS_API_URL") or TOWNLOOPS_DEFAULT_BASE).rstrip("/")
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
                "Idempotency-Key": f"hermes-resync-orphan:{kind}:{slug}",
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
                    "Idempotency-Key": f"hermes-resync-orphan:{kind}:{slug}",
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
    base = (os.getenv("TOWNLOOPS_API_URL") or TOWNLOOPS_DEFAULT_BASE).rstrip("/")
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
