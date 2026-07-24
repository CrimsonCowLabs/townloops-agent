"""Shared ingest primitives — dataclasses, registry, utils, PARSERS.

Extracted from `event-ingest/ingest.py`. Phase 1 (2026-07-08) moved the
dataclasses + pure utils + hashing. Phase 2 (2026-07-09) added the parsers
and HTTP fetch helpers. Both are drift insurance — the kernel now lives in
one place so a bug fix lands once and both this town-agent's ingest.py AND
SLT's events.py can eventually import it.

Contents:
  * Event/Venue/Business/Community/Performer/Creator/MinedEntity dataclasses.
    Field lists match the OpenAPI payload contracts.
  * ENTITY_REG — kind → {table, endpoint, cls, cols, hash_cols}.
  * Pure text utilities (`_clean`, `_truncate`, `_norm`).
  * Venue-guessing heuristics (`_venue_from_location`,
    `_extract_venue_from_description`, `_should_replace_venue`).
  * Timezone-aware helpers (`_now_pt`, `_iso_now`, `_parse_dt`, `_iso`)
    defaulting to `DEFAULT_TZ` (env-configurable via AGENT_TZ).
  * Deterministic identity + content hashing.
  * HTTP fetch helpers (`_http_get`, `_firecrawl_scrape`, `_fetch_for_enrichment`).
  * Parsers (`parse_ical`, `parse_jsonld`, `parse_rss`, `parse_selectors`,
    `parse_web`) + the PARSERS registry.
  * Enrichment overlay (`_extract_enrichment_field`, `_apply_enrichments`).

Callers keep using the leading-underscore names — they're the public API here.

**NOT extracted (per-deployment forever)**: DB upserts, ship logic (deeply
tied to town-scoped config), CLI subcommands. Those stay in `ingest.py`.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from dateutil import tz
from icalendar import Calendar


# =====================================================================
# Timezone
# =====================================================================
#
# Callers usually pick up DEFAULT_TZ implicitly. If a caller wires up
# a different tz (e.g. events.py wants PT specifically for legacy SLT
# behavior), it can pass tz_=... to any of the helpers below or reassign
# ingest_core.DEFAULT_TZ at import time.

DEFAULT_TZ = tz.gettz(os.environ.get("AGENT_TZ", "America/Los_Angeles"))


def _now_pt(tz_=None) -> datetime:
    """Wall-clock now, tz-aware. Historical name kept — `_pt` no longer means
    Pacific specifically, it means "primary timezone for this deployment."""
    return datetime.now(tz=tz_ or DEFAULT_TZ)


def _iso_now(tz_=None) -> str:
    return _now_pt(tz_).isoformat()


def _iso(d: datetime) -> str:
    return d.isoformat()


def _parse_dt(value, tz_=None) -> datetime | None:
    """Best-effort datetime parser. Attaches `tz_` (or DEFAULT_TZ) as tzinfo
    on naive parses. Returns None if the string is unparseable even with
    dateutil's fuzzy=True."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        d = value
    else:
        s = str(value)
        try:
            d = dateparser.parse(s)
        except (ValueError, TypeError, OverflowError):
            # Strict parse failed — many real-world strings include decorative
            # tokens (e.g. "July 6, 2026 @ 7:30 p.m.") that fuzzy=True tolerates
            # by ignoring the unknown tokens.
            try:
                d = dateparser.parse(s, fuzzy=True)
            except (ValueError, TypeError, OverflowError):
                return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz_ or DEFAULT_TZ)
    return d


# =====================================================================
# Text utils — pure, no dependencies beyond stdlib
# =====================================================================


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


# =====================================================================
# Venue-guessing heuristics — pure functions over strings
# =====================================================================


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


# =====================================================================
# Dataclasses — Townloops payload mirrors
# =====================================================================


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


# Entity types — Townloops Phase B/C payload mirrors. Each dataclass matches
# a Townloops payload contract. Mining produces these from already-parsed
# events (see _mine_entities). Shared audit fields (source/source_url/extra)
# live on every dataclass so _upsert_entity can be generic over the kind.


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
    image_path: str = ""
    image_alt: str = ""
    instagram: str = ""
    facebook: str = ""
    tiktok: str = ""
    twitter: str = ""
    youtube: str = ""
    linkedin: str = ""
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
    image_path: str = ""
    image_alt: str = ""
    instagram: str = ""
    facebook: str = ""
    tiktok: str = ""
    twitter: str = ""
    youtube: str = ""
    linkedin: str = ""
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
    facebook: str = ""
    tiktok: str = ""
    twitter: str = ""
    youtube: str = ""
    linkedin: str = ""
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
    facebook: str = ""
    tiktok: str = ""
    twitter: str = ""
    linkedin: str = ""
    source: str = ""
    source_url: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class MinedEntity:
    kind: str          # "venue" | "business" | "community" | "performer" | "creator"
    data: object       # one of the five dataclasses above


# =====================================================================
# Entity registry
# =====================================================================


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
        "cols": ["name", "description", "address", "website", "phone", "category_slug",
                 "image_path", "image_alt",
                 "instagram", "facebook", "tiktok", "twitter", "youtube", "linkedin"],
    },
    "community": {
        "table": "communities",
        "endpoint": "communities",
        "cls": Community,
        "cols": ["name", "kind", "description", "website", "meeting_info", "category_slug",
                 "image_path", "image_alt",
                 "instagram", "facebook", "tiktok", "twitter", "youtube", "linkedin"],
    },
    "performer": {
        "table": "performers",
        "endpoint": "performers",
        "cls": Performer,
        "cols": ["name", "kind", "description", "website", "instagram", "spotify_url",
                 "image_path", "image_alt",
                 "facebook", "tiktok", "twitter", "youtube", "linkedin"],
    },
    "creator": {
        "table": "creators",
        "endpoint": "creators",
        "cls": Creator,
        "cols": ["name", "kind", "bio", "website", "instagram", "youtube_url",
                 "image_path", "image_alt",
                 "facebook", "tiktok", "twitter", "linkedin"],
    },
}
# All payload cols participate in content_hash.
for _spec in ENTITY_REG.values():
    _spec["hash_cols"] = list(_spec["cols"])
ENTITY_KINDS = list(ENTITY_REG.keys())


# =====================================================================
# Identity + content hashing — pure functions over dataclasses
# =====================================================================


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


# =====================================================================
# HTTP fetch layer
# =====================================================================


FIRECRAWL_CLOUD_BASE = "https://api.firecrawl.dev"
DEFAULT_MAX_DETAIL_FETCHES = 50


def _http_get(url: str, timeout: int = 20) -> requests.Response:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; HermesEventAggregator/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml,text/calendar,application/rss+xml,*/*",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r


def _firecrawl_endpoint() -> str:
    """Honor FIRECRAWL_API_URL (self-hosted) if set, else fall back to the cloud."""
    base = (os.getenv("FIRECRAWL_API_URL") or FIRECRAWL_CLOUD_BASE).rstrip("/")
    return f"{base}/v1/scrape"


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


# =====================================================================
# JSON-LD walker + Event synthesis
# =====================================================================


def _walk_jsonld(blob) -> Iterable[dict]:
    if isinstance(blob, dict):
        yield blob
        # The general "for v in blob.values()" loop below already recurses
        # into `@graph` if present. Previously there was a separate explicit
        # `@graph` handler here — that double-yielded every graph item.
        # Downstream dedupe (Event.dedupe_key) silently collapsed the duplicates
        # so it wasn't a functional bug, but it doubled the parser's work on
        # every `@graph`-carrying page. Test coverage caught the redundancy
        # 2026-07-09; consolidated to a single traversal.
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


# =====================================================================
# CSS-selector parsing (BS4)
# =====================================================================


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


# =====================================================================
# Top-level parsers
# =====================================================================


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
                dt_val = dt_val.replace(tzinfo=DEFAULT_TZ)
        else:
            dt_val = datetime.combine(dt_val, datetime.min.time()).replace(tzinfo=DEFAULT_TZ)
        end_iso = ""
        dtend = comp.get("dtend")
        if dtend is not None:
            ev = dtend.dt
            if isinstance(ev, datetime):
                if ev.tzinfo is None:
                    ev = ev.replace(tzinfo=DEFAULT_TZ)
            else:
                ev = datetime.combine(ev, datetime.min.time()).replace(tzinfo=DEFAULT_TZ)
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


def parse_selectors(source: dict) -> list[Event]:
    r = _http_get(source["url"])
    return _extract_selectors_from_html(r.text, source, source["url"])


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


# =====================================================================
# Enrichment overlay
# =====================================================================
# Per-source `enrichment_urls` overlay additional fields onto events whose
# name matches. Use when a primary listing source has minimal detail (e.g.
# JSON-LD with just title/date) and per-event detail pages carry the
# synopsis / accessibility notes / etc.
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
        for field_name, spec in sel_specs.items():
            value = _extract_enrichment_field(soup, spec)
            if value:
                # Conservative truncation: descriptions go up to 1500 chars,
                # other fields keep their natural length.
                limit = 1500 if field_name == "description" else 1000
                extracted[field_name] = _clean(value, limit)
        if not extracted:
            print(f"[warn] enrichment {url}: matched 0 selectors", file=sys.stderr)
            continue
        overwrite = enr.get("overwrite", True)
        applied = 0
        for ev in events:
            if ev.name.strip().lower() != match_name:
                continue
            for field_name, value in extracted.items():
                cur = getattr(ev, field_name, None)
                if overwrite or not cur:
                    setattr(ev, field_name, value)
            applied += 1
        print(f"[enrich] {url} → {applied} events ({', '.join(extracted)})", file=sys.stderr)
