"""Entity enrichment — resolve a venue/business/community named on an event to
a local row (local match → live mirror → new shell), and research any of the
five entity kinds from its own site to fill blank fields, under a lookup
budget.

Never writes to TownLoops. Live rows are mirrored locally; an image found for
a live row without one is staged as a proposal (`image-proposed`) for the
operator, never filled into other fields; a live row whose site shows no
image (`image-not-found`) is looked at again only after
LIVE_IMAGE_RETRY_DAYS. A lookup the budget cut short leaves the entity
`pending` for the next run. Researched values are verbatim from
the page (no synthesized copy) and are validated against
`entity_payload.schema.json` before they touch a row.

Fields with no structured source (a community's `meeting_info`, a venue's
`accessibility_notes`) are picked from the site's text by one LLM call
(engine.page_extract): only sentences found on the page survive. Venue sites
often keep access info off the homepage, so a venue's read also takes the
site's own accessibility (else visit, else FAQ) page when the homepage links
one and the budget allows. A failed or throttled call defers the entity —
the site's other fields are filled, but its status stays as it was, so a
later run reads it again (it was not looked at, so it is not `not-found`).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from engine import entity_classify, listing_hosts, page_extract
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

# When a quote is about meeting: a day, a frequency, a clock time, or the
# meeting itself ("Meetings are held in room 102").
_MEETING_TERMS = re.compile(
    r"\bmeet(?:s|ing|ings)?\b|\bgathers?\b"
    r"|\b(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?s?\b"
    r"|\b(?:daily|weekly|bi-?weekly|monthly|bi-?monthly|quarterly|annually)\b"
    r"|\b(?:every|each)\s+(?:other\s+)?(?:day|week|month|year|morning|evening)\b"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\b\.?|p\.?m\b\.?)|\b\d{1,2}:\d{2}\b|\bnoon\b",
    re.IGNORECASE,
)
MEETING_INFO = page_extract.FieldSpec(
    hint=("when and where this group regularly meets (day or frequency, time, "
          "place) — its own meetings, not one-off events or office hours"),
    keep=lambda quote: bool(_MEETING_TERMS.search(quote)),
    max_chars=1000,
)
# When a quote is about physical access at a venue. Stricter than an event
# page's check (engine/event_detail): a bare "ADA compliant", "people with
# disabilities" or a website-accessibility statement — site-footer
# boilerplate — names no access feature. No bare "lift" (ski lifts).
_VENUE_ACCESS_TERMS = re.compile(
    r"wheel\s?chair|step[- ]free|\bramps?\b|level[- ](?:entry|entrance|access)"
    r"|\bno (?:stairs|steps)\b|elevator|hearing[- ]loop|induction[- ]loop"
    r"|assist(?:ed|ive)[- ]listening|\bASL\b|sign[- ]language|caption|audio[- ]descri"
    r"|braille|large[- ]print|sensory|quiet[- ](?:room|space|area)|service[- ](?:animal|dog)"
    r"|companion[- ]seat|grab[- ]bars?"
    r"|accessible[- ](?:seat|parking|entr|restroom|bathroom|toilet|route|viewing|space|stall"
    r"|room|path)"
    r"|(?:ADA|handicap(?:ped)?|disabled|disability)[- ](?:parking|seat|restroom|bathroom"
    r"|toilet|stall|entr|space|ramp)",
    re.IGNORECASE,
)
_WEB_ACCESS = re.compile(
    r"\bweb\s?sites?\b|digital accessib|\bWCAG\b|screen[- ]readers?|\bthis site\b",
    re.IGNORECASE,
)


def _venue_access(quote: str) -> bool:
    return bool(_VENUE_ACCESS_TERMS.search(quote)) and not _WEB_ACCESS.search(quote)


VENUE_ACCESSIBILITY = page_extract.FieldSpec(
    hint=("physical accessibility at this venue (step-free or ramped entry, accessible "
          "restrooms, elevators, accessible seating or parking, hearing loops or assisted "
          "listening, service animals) — not a website accessibility statement or a bare "
          "\"ADA compliant\" line"),
    keep=_venue_access,
    max_chars=2000,
)
# Per kind: fields read from the site's text by the LLM (fill-blanks only).
PAGE_FIELDS: dict[str, dict[str, page_extract.FieldSpec]] = {
    "community": {"meeting_info": MEETING_INFO},
    "venue": {"accessibility_notes": VENUE_ACCESSIBILITY},
}

# Kinds resolve_entity() can resolve by name from an event.
TABLES = {k: SPECS[k].table for k in ("venue", "business", "community")}
# Fields research_entity() returns when no kind is given.
ENRICH_FIELDS = ("description", "address", "website", "phone", "image_path",
                 "image_alt", *_SOCIALS)
# enrichment_status values that mean "already researched — don't repeat".
DONE_STATUSES = ("enriched", "not-found", "image-proposed", "image-not-found", "live",
                 "needs-review")
# A live mirror whose site showed no image is researched again after this.
LIVE_IMAGE_RETRY_DAYS = 90


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
    # A created shell's enrichment_status (`pending` when the budget cut its
    # lookup short).
    status: Optional[str] = None


def _pick_site(name: str, results: list[dict]) -> Optional[str]:
    """First search hit that is plausibly the entity's own site — never a
    listing page (host_blocked covers engine.listing_hosts)."""
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


# On-site pages that may hold a venue's access info, best first.
_ACCESS_LINKS = (
    re.compile(r"accessib|\bada\b|disabilit", re.IGNORECASE),
    re.compile(r"\bvisit(?:ing|ors?)?\b|getting here|directions", re.IGNORECASE),
    re.compile(r"\bfaqs?\b|frequently asked", re.IGNORECASE),
)
_NOT_A_PAGE = re.compile(r"\.(?:pdf|docx?|jpe?g|png|gif|webp|mp4|zip)$", re.IGNORECASE)


def access_page(links: list[tuple[str, str]], site_url: str) -> Optional[str]:
    """The site's own accessibility page among `links` ((url, anchor text)
    from the page at `site_url`), else its visit page, else its FAQ; None
    when it links none. Other sites, the page itself and files are skipped.
    A venue whose site is a page on a larger one (a city's /lakeview) only
    follows links under that page — the city's own FAQ is not the venue's."""
    root = urlparse(site_url).path.rstrip("/")
    candidates = []
    for url, text in links:
        p = urlparse(url.split("#")[0])
        path = p.path.rstrip("/")
        if (listing_hosts.bare_host(url) != listing_hosts.bare_host(site_url)
                or path == root
                or (root and not path.startswith(root + "/"))
                or _NOT_A_PAGE.search(p.path)):
            continue
        url = p.geturl()
        words = f"{text} {re.sub(r'[-_/.]+', ' ', p.path)}"
        for rank, pattern in enumerate(_ACCESS_LINKS):
            if pattern.search(words):
                candidates.append((rank, url))
                break
    return min(candidates, key=lambda c: c[0])[1] if candidates else None


@dataclass
class Lookup:
    payload: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    text: str = ""              # the visible text of the pages read
    # The entity's site wasn't read — budget ran out, search errored or the
    # page couldn't be fetched — so an empty payload means "not looked at",
    # not "not found".
    exhausted: bool = False
    # The site was read but the LLM call for its PAGE_FIELDS failed: the
    # payload is good, the entity is still to be looked at again.
    page_deferred: bool = False


def _lookup(name: str, url: Optional[str], town: str, budget: Budget,
            kind: Optional[str], read_access_page: bool = False) -> Lookup:
    """Find and read the entity's site. With `read_access_page`, the text of
    the site's own accessibility/visit/FAQ page (access_page) is added too,
    if the budget has a call left — it only feeds PAGE_FIELDS."""
    from engine import discovery, research
    if url and listing_hosts.is_listing_url(url):
        # A listing page on file is not the entity's site — look for that.
        url = None
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
    texts: list[str] = []
    links: list[tuple[str, str]] = []
    for page in fetch_plan(url):
        if not budget.take():
            if not fetched:
                return Lookup(exhausted=True)
            break
        res = research.research_url(page, use_firecrawl=True)
        fetched.extend(res.fetched_from)
        if getattr(res, "text", ""):
            texts.append(res.text)
        if not links:
            links = getattr(res, "links", [])
        for k, v in res.payload.items():
            if v:
                raw.setdefault(k, v)
    if not fetched:
        # The site is known but couldn't be read (down, blocked, rate
        # limited) — that's not "not found"; leave it for a retry.
        return Lookup(errors=[f"could not read {url}"], exhausted=True)
    unread: list[str] = []
    if read_access_page and not _is_facebook(fetched[0]):
        extra = access_page(links, fetched[0])
        if extra and budget.take():
            text = research.fetch_text(extra, use_firecrawl=True)
            if text:
                texts.append(text)
            else:
                # Not deferred: a dead link would re-read the site forever.
                unread.append(f"could not read access page {extra}")
    website = _own_website(raw.pop("website", None), url)
    if website and _is_facebook(website):
        # A Facebook page is the entity's facebook, not its website.
        raw.setdefault("facebook", url)
    elif website:
        raw["website"] = website
    payload, errors = validate_payload(kind or "venue", raw)
    errors += unread
    text = "\n\n".join(texts)
    if kind is None:
        return Lookup({k: v for k, v in payload.items() if k in ENRICH_FIELDS}, errors, text=text)
    if kind == "creator":
        for src, dst in (("description", "bio"), ("youtube", "youtube_url")):
            if src in payload:
                payload.setdefault(dst, payload.pop(src))
    return Lookup({k: v for k, v in payload.items() if k in SPECS[kind].fillable}, errors,
                  text=text)


def _own_website(page_url: Optional[str], url: str) -> Optional[str]:
    """The website a read of `url` gives: the page's own canonical URL, else
    `url` — never a listing page (engine.listing_hosts). A listing page can
    be read as evidence; its URL is not the entity's website."""
    for candidate in (page_url, url):
        if candidate and not listing_hosts.is_listing_url(candidate):
            return candidate
    return None


def _research(row: dict, kind: str, town: str, budget: Budget,
              llm: Optional[entity_classify.LLM]) -> Lookup:
    """Look the entity up, then have `llm` read its PAGE_FIELDS that are
    still blank on `row` from the site's text. An LLM call that failed or
    was throttled marks the lookup `page_deferred`. With no `llm`,
    PAGE_FIELDS are not read."""
    wanted = {k: h for k, h in PAGE_FIELDS.get(kind, {}).items() if not row.get(k)}
    known = next((u for u in (row.get("website"), row.get("facebook"))
                  if u and not listing_hosts.is_listing_url(u)), None)
    found = _lookup(row["name"], known, town, budget, kind,
                    read_access_page="accessibility_notes" in wanted and llm is not None)
    if found.exhausted or not wanted or not found.text or llm is None:
        return found
    values = page_extract.extract(found.text, wanted, llm, subject=row["name"])
    if values is None:
        found.errors.append(f"LLM unavailable for {', '.join(wanted)}")
        found.page_deferred = True
        return found
    read, errors = validate_payload(kind, {k: v for k, v in values.items() if v})
    found.payload.update(read)
    found.errors.extend(errors)
    return found


def _fill_researched(conn: sqlite3.Connection, kind: str, row: dict, found: Lookup) -> list[str]:
    """Fill blanks from a lookup that read the site. A `page_deferred` one
    keeps the row's status so a later run reads the site again."""
    if found.page_deferred:
        status = row.get("enrichment_status") or "pending"
    else:
        status = "enriched" if found.payload else "not-found"
    return fill_blanks(conn, kind, row["entity_uid"], found.payload, status)


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
                  budget: Budget, llm: Optional[entity_classify.LLM] = None) -> EnrichOutcome:
    """Research one entity of any kind and fill its blank fields.

    - skipped: live mirrors (their values are TownLoops') and rows already
      researched (DONE_STATUSES) — the latter still get a blank category
      classified;
    - deferred: the entity's site wasn't read (budget ran out, search
      errored, page unreadable) — status stays as it was, so a later run
      can retry; or it was read but the LLM call for its PAGE_FIELDS
      failed — the other fields are filled, the status still stays;
    - not-found: no plausible site found — recorded as the row's status;
    - enriched: the site was read; blank fields filled.

    Once researched, a blank category is classified from the town's
    vocabulary (engine.entity_classify; `llm` is the fallback for names the
    keyword map can't place). `llm` also reads the kind's PAGE_FIELDS.
    """
    row = conn.execute(f"SELECT * FROM {SPECS[kind].table} WHERE entity_uid = ?",
                       (entity_uid,)).fetchone()
    if not row:
        return EnrichOutcome("skipped")
    row = dict(row)
    if row.get("source_kind") == "live":
        return EnrichOutcome("skipped")
    if row.get("enrichment_status") in DONE_STATUSES:
        # Researched already — but a category a failed or capped LLM call
        # left blank is retried.
        if entity_classify.classify_entity(conn, kind, entity_uid, llm):
            return EnrichOutcome("skipped", ["category_slug"])
        return EnrichOutcome("skipped")
    found = _research(row, kind, town, budget, llm)
    if found.exhausted:
        return EnrichOutcome("deferred", errors=found.errors)
    filled = _fill_researched(conn, kind, row, found)
    if found.page_deferred:
        return EnrichOutcome("deferred", filled, found.errors)
    status = "enriched" if found.payload else "not-found"
    if entity_classify.classify_entity(conn, kind, entity_uid, llm):
        filled.append("category_slug")
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
# same property. Listing hosts (engine.listing_hosts) are shared too.
_SHARED_HOSTS = ("facebook.com", "instagram.com", "linktr.ee", "sites.google.com",
                 "wixsite.com", "squarespace.com", "godaddysites.com", "google.com")


def _site(url: Optional[str]) -> str:
    """The property's own host for `url`, or "" for shared platforms."""
    host = listing_hosts.bare_host(url or "")
    if (any(host == h or host.endswith("." + h) for h in _SHARED_HOSTS)
            or listing_hosts.is_listing_host(host)):
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


def _retry_due(enriched_at: Optional[str]) -> bool:
    try:
        last = datetime.fromisoformat(enriched_at or "")
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last >= timedelta(days=LIVE_IMAGE_RETRY_DAYS)


def _propose_live_image(conn: sqlite3.Connection, kind: str, mirror: dict, town: str,
                        budget: Budget) -> tuple[list[str], Optional[dict]]:
    """Look for an image for a live mirror that has none. Only the image is
    staged (ship never sends a mirror's other fields). Returns (filled,
    proposal). A lookup the budget cut short leaves the mirror as it was; a
    site with no image backs off for LIVE_IMAGE_RETRY_DAYS."""
    status = mirror.get("enrichment_status")
    if mirror.get("image_path") or status == "image-proposed":
        return [], None
    if status == "image-not-found" and not _retry_due(mirror.get("enriched_at")):
        return [], None
    found = _lookup(mirror["name"], mirror.get("website"), town, budget, kind)
    if found.exhausted:
        return [], None
    image = {k: found.payload[k] for k in ("image_path", "image_alt") if found.payload.get(k)}
    if not image.get("image_path"):
        fill_blanks(conn, kind, mirror["entity_uid"], {}, "image-not-found")
        return [], None
    filled = fill_blanks(conn, kind, mirror["entity_uid"], image, "image-proposed")
    return filled, {"kind": kind, "slug": mirror["townloops_slug"], "image_path": image["image_path"]}


def _research_local(conn: sqlite3.Connection, kind: str, entity_uid: str, town: str,
                    budget: Budget, llm: Optional[entity_classify.LLM]) -> list[str]:
    """Research a local (non-mirror) entity and fill its blanks. Only a
    lookup that actually ran records `enriched`/`not-found`; one the budget
    (or a failed LLM call) cut short leaves the status (`pending`) for the
    next run."""
    row = dict(conn.execute(f"SELECT * FROM {SPECS[kind].table} WHERE entity_uid = ?",
                            (entity_uid,)).fetchone())
    found = _research(row, kind, town, budget, llm)
    if found.exhausted:
        return []
    return _fill_researched(conn, kind, row, found)


def resolve_entity(conn: sqlite3.Connection, kind: str, name: str, event: dict,
                   live_rows: list[dict], town: str, budget: Budget,
                   llm: Optional[entity_classify.LLM] = None) -> Resolution:
    """Resolve `name` (a `kind` named on `event`) to a local entity_uid:

    - local match → researched once if it has no image (a live mirror only
      gets an image proposal);
    - live match (from `live_rows`) → mirrored locally; if the live row has
      no image, one found on its site is staged as a proposal;
    - otherwise → a new derived shell, enriched from a site search.

    `llm` reads the kind's PAGE_FIELDS when a local row is researched.
    """
    table = TABLES[kind]
    local_rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    hit = best_name_match(name, local_rows)
    if hit:
        if hit.get("source_kind") == "live":
            filled, proposal = _propose_live_image(conn, kind, hit, town, budget)
            return Resolution(hit["entity_uid"], "local", filled, proposal)
        filled = []
        if not hit.get("image_path") and hit.get("enrichment_status") not in DONE_STATUSES:
            filled = _research_local(conn, kind, hit["entity_uid"], town, budget, llm)
        return Resolution(hit["entity_uid"], "local", filled)

    live_hit = best_name_match(name, live_rows)
    if live_hit:
        entity_uid = mirror_live_entity(conn, kind, live_hit)
        mirror = dict(conn.execute(f"SELECT * FROM {table} WHERE entity_uid = ?",
                                   (entity_uid,)).fetchone())
        filled, proposal = _propose_live_image(conn, kind, mirror, town, budget)
        return Resolution(entity_uid, "live", filled, proposal)

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
    filled = _research_local(conn, kind, entity_uid, town, budget, llm)
    status = conn.execute(f"SELECT enrichment_status FROM {table} WHERE entity_uid = ?",
                          (entity_uid,)).fetchone()[0]
    return Resolution(entity_uid, "created", filled, status=status)
