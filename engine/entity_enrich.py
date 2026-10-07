"""Entity enrichment — resolve a venue/business/community named on an event to
a local row (local match → live mirror → new shell), and research any of the
five entity kinds from its own site to fill blank fields, under a lookup
budget.

Never writes to TownLoops. Live rows are mirrored locally; an image found for
a live row without one is staged as a proposal (`image-proposed`) for the
operator, never filled into other fields. Researched values are verbatim from
the page (no synthesized copy) and are validated against
`entity_payload.schema.json` before they touch a row.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from engine.derive_helpers import (
    best_name_match, insert_entity, iso_utc_now, mint_entity_uid, mirror_live_entity,
    names_match, normalize_name,
)
from engine.entity_sync import refresh_content_hash


@dataclass(frozen=True)
class KindSpec:
    table: str
    fillable: tuple[str, ...]   # columns enrichment may fill when blank


_SOCIALS = ("instagram", "facebook", "tiktok", "twitter", "youtube", "linkedin")

# Restored from the pre-rewrite engine/enrichment.py. Names follow the
# TownLoops payload contract: creators have `bio` (not description) and
# `youtube_url` (not youtube).
SPECS: dict[str, KindSpec] = {
    "venue": KindSpec("venues",
                      ("description", "address", "website", "phone", "accessibility_notes",
                       "image_path", "image_alt")),
    "business": KindSpec("businesses",
                         ("description", "address", "website", "phone", "image_path",
                          "image_alt", *_SOCIALS)),
    "community": KindSpec("communities",
                          ("description", "website", "meeting_info", "image_path",
                           "image_alt", *_SOCIALS)),
    "performer": KindSpec("performers",
                          ("description", "website", "spotify_url", "image_path",
                           "image_alt", *_SOCIALS)),
    "creator": KindSpec("creators",
                        ("bio", "website", "youtube_url", "image_path", "image_alt",
                         "instagram", "facebook", "tiktok", "twitter", "linkedin")),
}

# Kinds resolve_entity() can resolve by name from an event.
TABLES = {k: SPECS[k].table for k in ("venue", "business", "community")}
# Fields research_entity() returns when no kind is given.
ENRICH_FIELDS = ("description", "address", "website", "phone", "image_path",
                 "image_alt", *_SOCIALS)
# enrichment_status values that mean "already researched — don't repeat".
DONE_STATUSES = ("enriched", "not-found", "image-proposed", "live", "needs-review")


class Budget:
    """Cap on Firecrawl search/scrape calls; each call takes one."""

    def __init__(self, n: int):
        self.left = n

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


@dataclass
class Resolution:
    entity_uid: str
    outcome: Literal["local", "live", "created"]
    filled: list[str] = field(default_factory=list)
    # Set when a live row without an image got one staged locally.
    image_proposal: Optional[dict] = None


def _pick_site(name: str, results: list[dict]) -> Optional[str]:
    """First search hit that is plausibly the entity's own site."""
    from engine.discovery import host_blocked
    compact = normalize_name(name).replace(" ", "")
    for r in results:
        url = r.get("url") or ""
        if not url or host_blocked(url, allow_gov=True):
            continue
        host = (urlparse(url).hostname or "").replace("-", "").replace(".", "")
        title = (r.get("title") or "").split("|")[0].split(" - ")[0]
        if names_match(name, title) or (len(compact) >= 5 and compact[:12] in host):
            return url
    return None


def _is_facebook(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "facebook.com" or host.endswith(".facebook.com")


def fetch_plan(url: str) -> list[str]:
    """Pages to read for an entity whose site is `url`. A Facebook page's
    contact details and LocalBusiness JSON-LD live on its /about/ tab."""
    p = urlparse(url)
    segments = [s for s in p.path.split("/") if s]
    if (_is_facebook(url) and len(segments) == 1
            and segments[0] not in ("profile.php", "events", "groups", "about")):
        return [url, f"{p.scheme}://{p.netloc}/{segments[0]}/about/"]
    return [url]


@dataclass
class Lookup:
    payload: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # The entity's site wasn't read — budget ran out, search errored or the
    # page couldn't be fetched — so an empty payload means "not looked at",
    # not "not found".
    exhausted: bool = False


def _lookup(name: str, url: Optional[str], town: str, budget: Budget,
            kind: Optional[str]) -> Lookup:
    from engine import discovery, research
    if not url:
        if not budget.take():
            return Lookup(exhausted=True)
        try:
            hits = discovery.firecrawl_search(f'"{name}" {town}', limit=5, raise_on_error=True)
        except (discovery.FirecrawlError, TimeoutError) as e:
            return Lookup(errors=[f"search failed: {e}"], exhausted=True)
        url = _pick_site(name, hits)
        if not url:
            return Lookup()
    raw: dict = {}
    fetched: list[str] = []
    for page in fetch_plan(url):
        if not budget.take():
            if not fetched:
                return Lookup(exhausted=True)
            break
        res = research.research_url(page, use_firecrawl=True)
        fetched.extend(res.fetched_from)
        for k, v in res.payload.items():
            if v:
                raw.setdefault(k, v)
    if not fetched:
        # The site is known but couldn't be read (down, blocked, rate
        # limited) — that's not "not found"; leave it for a retry.
        return Lookup(errors=[f"could not read {url}"], exhausted=True)
    raw.setdefault("website", url)
    if _is_facebook(raw["website"]):
        # A Facebook page is the entity's facebook, not its website.
        raw.setdefault("facebook", url)
        del raw["website"]
    payload, errors = validate_payload(kind or "venue", raw)
    if kind is None:
        return Lookup({k: v for k, v in payload.items() if k in ENRICH_FIELDS}, errors)
    if kind == "creator":
        for src, dst in (("description", "bio"), ("youtube", "youtube_url")):
            if src in payload:
                payload.setdefault(dst, payload.pop(src))
    return Lookup({k: v for k, v in payload.items() if k in SPECS[kind].fillable}, errors)


def research_entity(name: str, website: Optional[str], town: str, budget: Budget,
                    kind: Optional[str] = None) -> dict:
    """Find (search, if no website) and read the entity's site. Returns a
    validated payload of `kind`'s fillable fields (ENRICH_FIELDS when no
    kind is given); {} when nothing was found or the budget ran out."""
    return _lookup(name, website, town, budget, kind).payload


_SCHEMA_PATH = Path(__file__).with_name("entity_payload.schema.json")
_URL_FIELDS = {"website", "image_path", "spotify_url", "youtube_url"}


def validate_payload(kind: str, payload: dict) -> tuple[dict, list[str]]:
    """Check each value against the entity payload schema: a string (the
    schema's only field type), and an http(s) URL for link fields
    (https-only for image_path). Invalid values are dropped — null beats
    wrong. Returns (clean payload, one error per dropped field)."""
    props = json.loads(_SCHEMA_PATH.read_text())["properties"]["payload"]["properties"]
    clean: dict = {}
    errors: list[str] = []
    for key, value in payload.items():
        if not isinstance(value, str):
            errors.append(f"{kind}.{key}: expected a string, got {type(value).__name__}")
            continue
        if key in _URL_FIELDS or props.get(key, {}).get("format") == "uri":
            p = urlparse(value)
            schemes = ("https",) if key == "image_path" else ("http", "https")
            if p.scheme not in schemes or not p.netloc:
                errors.append(f"{kind}.{key}: not an {'/'.join(schemes)} URL: {value[:80]}")
                continue
        clean[key] = value
    return clean, errors


@dataclass
class EnrichOutcome:
    status: Literal["enriched", "not-found", "deferred", "skipped"]
    filled: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def enrich_entity(conn: sqlite3.Connection, kind: str, entity_uid: str, town: str,
                  budget: Budget) -> EnrichOutcome:
    """Research one entity of any kind and fill its blank fields.

    - skipped: live mirrors (their values are TownLoops') and rows already
      researched (DONE_STATUSES);
    - deferred: the entity's site wasn't read (budget ran out, search
      errored, page unreadable) — status stays as it was, so a later run
      can retry;
    - not-found: no plausible site found — recorded as the row's status;
    - enriched: the site was read; blank fields filled.
    """
    row = conn.execute(f"SELECT * FROM {SPECS[kind].table} WHERE entity_uid = ?",
                       (entity_uid,)).fetchone()
    if not row:
        return EnrichOutcome("skipped")
    row = dict(row)
    if row.get("source_kind") == "live" or row.get("enrichment_status") in DONE_STATUSES:
        return EnrichOutcome("skipped")
    found = _lookup(row["name"], row.get("website") or row.get("facebook"), town, budget, kind)
    if found.exhausted:
        return EnrichOutcome("deferred", errors=found.errors)
    status = "enriched" if found.payload else "not-found"
    filled = fill_blanks(conn, kind, entity_uid, found.payload, status)
    return EnrichOutcome(status, filled, found.errors)


def fill_blanks(conn: sqlite3.Connection, kind: str, entity_uid: str, payload: dict,
                status: str) -> list[str]:
    """Write payload values into the row's empty columns only, and record
    `status` as its enrichment_status. Returns the columns filled."""
    table = SPECS[kind].table
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info('{table}')")}
    row = dict(conn.execute(f"SELECT * FROM {table} WHERE entity_uid = ?", (entity_uid,)).fetchone())
    filled = [k for k, v in payload.items() if k in cols and v and not row.get(k)]
    sets = [f"{k} = ?" for k in filled]
    vals = [payload[k] for k in filled]
    if "enrichment_status" in cols:
        sets.append("enrichment_status = ?")
        vals.append(status)
    if "enriched_at" in cols:
        sets.append("enriched_at = ?")
        vals.append(iso_utc_now())
    if filled:
        sets.append("last_modified_at = ?")
        vals.append(iso_utc_now())
    if sets:
        conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE entity_uid = ?",
                     (*vals, entity_uid))
    if filled:
        # A changed hash is what sends the new values to an entity that is
        # already live (engine/entity_sync).
        refresh_content_hash(conn, kind, entity_uid)
    return filled


# Hosts many unrelated entities share — the same host there is not the
# same property.
_SHARED_HOSTS = ("facebook.com", "instagram.com", "linktr.ee", "sites.google.com",
                 "eventbrite.com", "wixsite.com", "squarespace.com", "godaddysites.com",
                 "yelp.com", "tripadvisor.com", "google.com")


def _site(url: Optional[str]) -> str:
    """The property's own host for `url`, or "" for shared platforms."""
    host = (urlparse(url or "").hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if any(host == h or host.endswith("." + h) for h in _SHARED_HOSTS):
        return ""
    return host


def borrow_property_image(conn: sqlite3.Connection, venue_uid: str,
                          others: list[tuple[str, str]]) -> Optional[str]:
    """A venue that is a room/stage inside a property (its website is on the
    property's site) and has no image of its own takes the property's image
    from a linked business/community on the same site. Returns the image
    borrowed, or None. Live mirrors are never touched."""
    venue = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (venue_uid,)).fetchone()
    if not venue or venue["image_path"] or venue["source_kind"] == "live" or not _site(venue["website"]):
        return None
    for kind, uid in others:
        if kind == "venue":
            continue
        other = conn.execute(f"SELECT name, website, image_path, image_alt FROM {SPECS[kind].table} "
                             "WHERE entity_uid = ?", (uid,)).fetchone()
        if other and other["image_path"] and _site(other["website"]) == _site(venue["website"]):
            conn.execute(
                "UPDATE venues SET image_path = ?, image_alt = COALESCE(image_alt, ?), "
                "last_modified_at = ? WHERE entity_uid = ? AND image_path IS NULL",
                (other["image_path"], other["image_alt"] or other["name"], iso_utc_now(), venue_uid),
            )
            refresh_content_hash(conn, "venue", venue_uid)
            return other["image_path"]
    return None


def resolve_entity(conn: sqlite3.Connection, kind: str, name: str, event: dict,
                   live_rows: list[dict], town: str, budget: Budget) -> Resolution:
    """Resolve `name` (a `kind` named on `event`) to a local entity_uid:

    - local match → enriched once if it has no image (live mirrors only get
      an image proposal);
    - live match (from `live_rows`) → mirrored locally; if the live row has
      no image, one found on its site is staged as a proposal;
    - otherwise → a new derived shell, enriched from a site search.
    """
    table = TABLES[kind]
    local_rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    hit = best_name_match(name, local_rows)
    if hit:
        filled: list[str] = []
        if not hit.get("image_path") and hit.get("enrichment_status") not in ("enriched", "not-found", "image-proposed"):
            payload = research_entity(hit["name"], hit.get("website"), town, budget, kind)
            if payload:
                status = "image-proposed" if hit.get("source_kind") == "live" else "enriched"
                filled = fill_blanks(conn, kind, hit["entity_uid"], payload, status)
        return Resolution(hit["entity_uid"], "local", filled)

    live_hit = best_name_match(name, live_rows)
    if live_hit:
        entity_uid = mirror_live_entity(conn, kind, live_hit)
        if live_hit.get("image_path"):
            return Resolution(entity_uid, "live")
        payload = research_entity(live_hit["name"], live_hit.get("website"), town, budget, kind)
        if not payload.get("image_path"):
            return Resolution(entity_uid, "live")
        filled = fill_blanks(conn, kind, entity_uid,
                             {k: payload[k] for k in ("image_path", "image_alt") if payload.get(k)},
                             "image-proposed")
        return Resolution(entity_uid, "live", filled,
                          {"kind": kind, "slug": live_hit["slug"], "image_path": payload["image_path"]})

    entity_uid = mint_entity_uid(kind, name, event["event_uid"])
    fields = {
        "name": name.strip(), "source": f"derived:event:{event['event_uid'][:12]}",
        "source_kind": "derived", "derived_from_event_uid": event["event_uid"],
        "derivation_confidence": 70, "enrichment_status": "pending",
        "category_slug": event.get("category_slug"),
    }
    if kind == "venue":
        fields["address"] = event.get("location") or event.get("location_raw")
    if kind == "community":
        fields["kind"] = "group"
    insert_entity(conn, table, entity_uid, fields)
    payload = research_entity(name, None, town, budget, kind)
    filled = fill_blanks(conn, kind, entity_uid, payload, "enriched" if payload else "not-found")
    return Resolution(entity_uid, "created", filled)
