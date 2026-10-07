"""Firecrawl-backed discovery kernel — the reusable pieces for finding
new businesses and communities in a town without hitting any LLM.

Lifted from the CMO's `townloops-business-discovery/discover.py` and
generalized: the kernel does not touch SQLite or town-profile config; skills
own that. The kernel returns plain dataclasses / dicts.

Callers:
  - engine/business_discovery.py (the business-discovery skill)

Firecrawl access rules (matches CMO version):
  - $FIRECRAWL_API_KEY set → use it (cloud or self-hosted-with-keys).
  - $FIRECRAWL_API_URL set (self-hosted, no key) → placeholder "Bearer
    self-hosted" satisfies the schema.
  - Neither → hard fail.

Rate limiting: every Firecrawl call goes through
`engine.policy.firecrawl_bucket.wait()` — configurable ceiling shared across
the process.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from engine.ingest_core import DEFAULT_TZ
from engine.firecrawl_env import self_hosted_url
from engine.policy import firecrawl_bucket


FIRECRAWL_DEFAULT_BASE = "https://api.firecrawl.dev"
FIRECRAWL_SEARCH_LIMIT = 10
FIRECRAWL_TIMEOUT_S = 45
BETWEEN_QUERIES_S = 0.5

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "_ga", "_gl",
}

HOST_BLOCKLIST_SUBSTRINGS = [
    "yelp.com", "tripadvisor.com", "tripadvisor.",
    "mapquest.com", "wikipedia.org", "wikivoyage.org", "wikitravel.org",
    "reddit.com", "pinterest.com",
    "instagram.com/explore", "linkedin.com/pub",
    "youtube.com/results", "google.com/maps", "bing.com/maps",
]

# `.gov` was in the CMO blocklist because government sites weren't in scope for
# business discovery. Community discovery WANTS them (municipal category),
# so it lives in a separate list and callers opt in via ALLOW_GOV=True.
GOV_HOST_SUFFIXES = [".gov", ".gov/"]

DIRECTORY_HOST_BLOCKLIST_SUBSTRINGS = [
    "pagesuite", "threshold360.com", "simpleviewinc.com", "simpleviewcms.com",
    "cloudfront.net", "issuu.com", "fliphtml5.com",
    "instagram.com", "facebook.com", "tiktok.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "threads.net",
]

DIRECTORY_PATH_BLOCKLIST_SUBSTRINGS = [
    "/map/", "/maps/",
    "/visitor-guide", "/visitors-guide",
    "/brochure", "/brochures", "/travel-guide",
    "/p/", "/reel/", "/share/",
]

DIRECTORY_SECTION_PATH_TOKENS = [
    "things-to-do", "dining", "nightlife", "restaurants", "shopping",
    "activities", "plan-your-trip", "explore-the-resort",
    "about-the-resort", "resort-info",
    "winter-activities", "summer-activities",
]

DIRECTORY_FILE_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".mp4", ".mov", ".zip",
}

CLOSURE_MARKERS = [
    "permanently closed", "business closed", "under new ownership",
    "has moved", "for lease", "closed for good", "no longer in business",
    "closed permanently", "space available",
]

CUSTOMER_FACING_MARKERS = [
    "menu", "book now", "reserve", "hours", "our services", "shop",
    "book online", "contact us", "location", "directions", "about us",
    "get in touch",
]


class FirecrawlError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------

def canonicalize_url(url: str) -> str:
    p = urlparse(url.strip())
    if not p.scheme:
        p = urlparse("https://" + url.strip())
    q = [
        (k, v)
        for (k, v) in parse_qsl(p.query, keep_blank_values=True)
        if k not in TRACKING_PARAMS
    ]
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path or "/"
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    return urlunparse((p.scheme.lower(), host, path, p.params, urlencode(q), ""))


def host_blocked(url: str, allow_gov: bool = False) -> bool:
    host = urlparse(url).netloc.lower()
    if any(bad in host for bad in HOST_BLOCKLIST_SUBSTRINGS):
        return True
    if not allow_gov and any(host.endswith(sfx) or (sfx in host) for sfx in GOV_HOST_SUFFIXES):
        return True
    return False


def directory_link_block_reason(seed_host: str, link: str) -> Optional[str]:
    p = urlparse(link)
    host = p.netloc.lower()
    path = (p.path or "/").lower()
    segments = [seg for seg in path.split("/") if seg]
    if not host:
        return "missing host"
    if host == seed_host:
        return "same host as seed"
    if host_blocked(link, allow_gov=True):
        return "host matches general blocklist"
    if any(bad in host for bad in DIRECTORY_HOST_BLOCKLIST_SUBSTRINGS):
        return "host matches directory junk blocklist"
    if any(path.endswith(ext) for ext in DIRECTORY_FILE_EXTENSIONS):
        return "file asset/media URL"
    if any(token in path for token in DIRECTORY_PATH_BLOCKLIST_SUBSTRINGS):
        return "directory utility/map/guide URL"
    generic_hits = sum(1 for seg in segments if seg in DIRECTORY_SECTION_PATH_TOKENS)
    if generic_hits >= 2:
        return "generic directory/resort section path"
    return None


# --------------------------------------------------------------------------
# Firecrawl client
# --------------------------------------------------------------------------

def _firecrawl_base() -> str:
    raw = (self_hosted_url() or FIRECRAWL_DEFAULT_BASE).rstrip("/")
    if raw.endswith("/v1"):
        raw = raw[:-3]
    return raw


def _firecrawl_headers() -> dict:
    key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    self_hosted = bool(self_hosted_url())
    if not key and not self_hosted:
        raise RuntimeError(
            "Firecrawl misconfigured: neither FIRECRAWL_API_KEY nor "
            "FIRECRAWL_API_URL is set."
        )
    return {
        "Authorization": f"Bearer {key or 'self-hosted'}",
        "Content-Type": "application/json",
    }


def firecrawl_search(query: str, *, limit: int = FIRECRAWL_SEARCH_LIMIT,
                     raise_on_error: bool = False) -> list[dict]:
    firecrawl_bucket.wait()
    try:
        r = requests.post(
            f"{_firecrawl_base()}/v1/search",
            headers=_firecrawl_headers(),
            json={"query": query, "limit": limit},
            timeout=FIRECRAWL_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        msg = f"[firecrawl-search][error] {query!r}: {type(exc).__name__}: {exc}"
        if raise_on_error:
            raise FirecrawlError(msg) from exc
        print(msg, file=sys.stderr)
        return []
    if not r.ok:
        msg = f"[firecrawl-search][http {r.status_code}] {query!r}: {r.text[:200]}"
        if raise_on_error:
            raise FirecrawlError(msg)
        print(msg, file=sys.stderr)
        return []
    try:
        body = r.json()
    except ValueError as exc:
        msg = f"[firecrawl-search][json] {query!r}: could not decode response body"
        if raise_on_error:
            raise FirecrawlError(msg) from exc
        print(msg, file=sys.stderr)
        return []
    data = body.get("data") or []
    return [
        {
            "url": d.get("url"),
            "title": d.get("title") or "",
            "description": d.get("description") or "",
        }
        for d in data
        if d.get("url")
    ]


def firecrawl_scrape(url: str, *, raise_on_error: bool = False) -> dict:
    firecrawl_bucket.wait()
    try:
        r = requests.post(
            f"{_firecrawl_base()}/v1/scrape",
            headers=_firecrawl_headers(),
            json={"url": url, "formats": ["markdown", "links"]},
            timeout=FIRECRAWL_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        msg = f"[firecrawl-scrape][error] {url}: {type(exc).__name__}: {exc}"
        if raise_on_error:
            raise FirecrawlError(msg) from exc
        print(msg, file=sys.stderr)
        return {}
    if not r.ok:
        msg = f"[firecrawl-scrape][http {r.status_code}] {url}: {r.text[:200]}"
        if raise_on_error:
            raise FirecrawlError(msg)
        print(msg, file=sys.stderr)
        return {}
    try:
        body = r.json()
    except ValueError as exc:
        msg = f"[firecrawl-scrape][json] {url}: could not decode response body"
        if raise_on_error:
            raise FirecrawlError(msg) from exc
        print(msg, file=sys.stderr)
        return {}
    data = body.get("data") or {}
    return {
        "markdown": data.get("markdown") or "",
        "links": data.get("links") or [],
        "title": ((data.get("metadata") or {}).get("title")) or "",
    }


# --------------------------------------------------------------------------
# Candidate dataclass
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    canonical_url: str
    display_name: Optional[str]
    source_signal: str
    kind: str = "business"  # 'business' | 'community'
    target_category_slug: Optional[str] = None


# --------------------------------------------------------------------------
# Discovery sources
# --------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(tz=DEFAULT_TZ).isoformat()


def sweep_web_search(
    town_name: str,
    region: str,
    query_templates: Iterable[str],
    *,
    kind: str = "business",
    per_source: int = 25,
    allow_gov: bool = False,
    error_sink: Optional[list[str]] = None,
) -> list[Candidate]:
    """Fan out a set of Firecrawl `/v1/search` queries and collect candidates.

    query_templates are Python format strings with named placeholders
    {town}, {region}, {category}. The caller loops through categories and
    passes the resolved template; here we accept an already-templated list
    so callers can inject arbitrary query shapes (`"nonprofit organizations
    in {town}"`, `"{category} in {town}, {region}"`, `"best {category}
    {town}"`, etc.).
    """
    out: list[Candidate] = []
    seen: set[str] = set()
    for q in query_templates:
        if len(out) >= per_source:
            break
        try:
            results = firecrawl_search(q, limit=FIRECRAWL_SEARCH_LIMIT, raise_on_error=True)
        except FirecrawlError as exc:
            if error_sink is not None:
                error_sink.append(str(exc))
            continue
        for res in results:
            url = res.get("url") or ""
            if not url or host_blocked(url, allow_gov=allow_gov):
                continue
            canon = canonicalize_url(url)
            if canon in seen:
                continue
            seen.add(canon)
            out.append(Candidate(
                canonical_url=canon,
                display_name=(res.get("title") or "").split(" - ")[0].strip()[:120] or None,
                source_signal=f"web-search:{q[:40]}",
                kind=kind,
            ))
            if len(out) >= per_source:
                break
        time.sleep(BETWEEN_QUERIES_S)
    return out


def sweep_directories(
    seed_urls: Iterable[str],
    *,
    kind: str = "business",
    per_source: int = 25,
    error_sink: Optional[list[str]] = None,
) -> list[Candidate]:
    """Scrape seed directory pages and collect the outbound links they surface.

    Each seed's linkset is filtered through directory_link_block_reason.
    """
    out: list[Candidate] = []
    seen: set[str] = set()
    for seed in seed_urls:
        if len(out) >= per_source:
            break
        seed_host = urlparse(seed).netloc.lower()
        try:
            scraped = firecrawl_scrape(seed, raise_on_error=True)
        except FirecrawlError as exc:
            if error_sink is not None:
                error_sink.append(str(exc))
            continue
        if not scraped:
            continue
        for link in scraped.get("links") or []:
            if len(out) >= per_source:
                break
            if not isinstance(link, str) or not link.startswith(("http://", "https://")):
                continue
            if directory_link_block_reason(seed_host, link):
                continue
            canon = canonicalize_url(link)
            if canon in seen:
                continue
            seen.add(canon)
            out.append(Candidate(
                canonical_url=canon,
                display_name=None,
                source_signal=f"directory:{seed_host}",
                kind=kind,
            ))
        time.sleep(BETWEEN_QUERIES_S)
    return out


def sweep_facebook(
    town_name: str,
    categories_or_terms: Iterable[str],
    *,
    kind: str = "business",
    per_source: int = 25,
    error_sink: Optional[list[str]] = None,
) -> list[Candidate]:
    """`site:facebook.com "{town}" {term}` search per term."""
    out: list[Candidate] = []
    seen: set[str] = set()
    for term in categories_or_terms:
        if not term:
            continue
        q = f'site:facebook.com "{town_name}" {term}'
        try:
            results = firecrawl_search(q, limit=FIRECRAWL_SEARCH_LIMIT, raise_on_error=True)
        except FirecrawlError as exc:
            if error_sink is not None:
                error_sink.append(str(exc))
            continue
        for res in results:
            url = res.get("url") or ""
            if "facebook.com" not in urlparse(url).netloc.lower():
                continue
            path = urlparse(url).path.lower()
            if any(seg in path for seg in ("/events/", "/groups/", "/photos/", "/reels/", "/watch/")):
                continue
            canon = canonicalize_url(url)
            if canon in seen:
                continue
            seen.add(canon)
            out.append(Candidate(
                canonical_url=canon,
                display_name=(res.get("title") or "").split(" | ")[0].strip()[:120] or None,
                source_signal="facebook",
                kind=kind,
            ))
            if len(out) >= per_source:
                break
        time.sleep(BETWEEN_QUERIES_S)
        if len(out) >= per_source:
            break
    return out


# --------------------------------------------------------------------------
# Verify — pure Firecrawl+heuristic (no LLM)
# --------------------------------------------------------------------------

_ADDRESS_HINT_RE = re.compile(
    r"\b\d{1,6}\s+[A-Z][A-Za-z0-9\.\- ]{2,60}\s+"
    r"(?:st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|way|hwy|highway|ln|lane|ct|court|pl|place|pkwy|parkway)\b",
    re.IGNORECASE,
)
_PHONE_HINT_RE = re.compile(r"\(?\b\d{3}\)?[\s\.\-]?\d{3}[\s\.\-]?\d{4}\b")
_HOURS_HINT_RE = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?\b[^.\n]{0,40}(?:am|pm)\b",
    re.IGNORECASE,
)


def verify_candidate(
    canonical_url: str,
    *,
    town_name: str,
    region: str = "",
    aliases: list[str] | None = None,
) -> dict:
    """Adversarial verify: scrape the candidate URL, decide whether it's a
    real customer-facing entity in the target town.

    Return shape matches `engine.policy.is_high_confidence()` expectations:
      {
        verified: bool,
        reasons: [str, ...],
        evidence: str,           # concat of head + address/phone/hours matches
        markdown_head: str,      # first ~500 chars for downstream inspection
        title: str,              # the page <title>; names a nameless candidate
        address: str | None,
        phone: str | None,
        hours: str | None,
        fetched_at: str,
      }
    """
    scraped = firecrawl_scrape(canonical_url)
    if not scraped:
        return {
            "verified": False,
            "reasons": ["scrape failed — page unreachable"],
            "fetched_at": _iso_now(),
        }
    md = scraped.get("markdown") or ""
    if not md:
        return {
            "verified": False,
            "reasons": ["scrape returned empty markdown"],
            "fetched_at": _iso_now(),
        }

    md_lower = md.lower()
    reasons_reject: list[str] = []

    tn = (town_name or "").lower()
    rg = (region or "").lower()

    # Build the list of "in-town" tokens to match. Start with the exact
    # town name + region, then extend with any operator-provided aliases
    # (from town-profile.yaml → name_aliases). Also treat region strings
    # containing "/" as multi-region ("California/Nevada" → ["california",
    # "nevada"]) so cross-state towns match either side.
    tokens: list[str] = []
    if tn:
        tokens.append(tn)
    for a in (aliases or []):
        a_lower = (a or "").strip().lower()
        if a_lower:
            tokens.append(a_lower)
    if rg:
        if "/" in rg:
            tokens.extend(part.strip() for part in rg.split("/") if part.strip())
        else:
            tokens.append(rg)

    in_town = False
    matched_token = None
    for tok in tokens:
        if re.search(rf"\b{re.escape(tok)}\b", md_lower):
            in_town = True
            matched_token = tok
            break
    if not in_town:
        # Short summary in the reject reason — the operator will see which
        # tokens the verifier tried and none matched.
        listed = ", ".join(repr(t) for t in tokens[:5]) or "(no tokens)"
        reasons_reject.append(f"no mention of any of {listed} on page")

    for marker in CLOSURE_MARKERS:
        if marker in md_lower:
            reasons_reject.append(f"closure marker found: {marker!r}")
            break

    customer_facing = any(marker in md_lower for marker in CUSTOMER_FACING_MARKERS)
    if not customer_facing:
        reasons_reject.append("no customer-facing markers (menu/hours/book/shop/services)")

    addr_m = _ADDRESS_HINT_RE.search(md)
    phone_m = _PHONE_HINT_RE.search(md)
    hours_m = _HOURS_HINT_RE.search(md)

    address = addr_m.group(0).strip() if addr_m else None
    phone = phone_m.group(0).strip() if phone_m else None
    hours = hours_m.group(0).strip() if hours_m else None

    evidence_parts = [md[:500]]
    for extracted in (address, phone, hours):
        if extracted:
            evidence_parts.append(extracted)
    evidence = " | ".join(evidence_parts)

    if reasons_reject:
        return {
            "verified": False,
            "reasons": reasons_reject,
            "evidence": evidence,
            "markdown_head": md[:500],
            "title": scraped.get("title") or "",
            "address": address,
            "phone": phone,
            "hours": hours,
            "fetched_at": _iso_now(),
        }
    return {
        "verified": True,
        "reasons": ["in-town, no closure marker, customer-facing"],
        "evidence": evidence,
        "markdown_head": md[:500],
        "title": scraped.get("title") or "",
        "address": address,
        "phone": phone,
        "hours": hours,
        "fetched_at": _iso_now(),
    }
