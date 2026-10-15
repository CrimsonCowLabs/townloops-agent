"""Firecrawl-backed discovery kernel — the reusable pieces for finding
new businesses and communities in a town without hitting any LLM.

Lifted from the CMO's `townloops-business-discovery/discover.py` and
generalized: the kernel does not touch SQLite or town-profile config; skills
own that. The kernel returns plain dataclasses / dicts.

Callers:
  - engine/business_discovery.py (the business-discovery skill)

Firecrawl access: the town's local instance only (engine.firecrawl_env) —
no local URL configured → FirecrawlNotConfigured, never cloud.

Rate limiting: every Firecrawl call goes through
`engine.policy.firecrawl_bucket.wait()` — configurable ceiling shared across
the process.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from engine.ingest_core import DEFAULT_TZ
from engine import firecrawl_env, listing_hosts, own_site
from engine.policy import firecrawl_bucket


FIRECRAWL_SEARCH_LIMIT = 10
FIRECRAWL_TIMEOUT_S = 45
BETWEEN_QUERIES_S = 0.5

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "_ga", "_gl",
}

# Directory/listing/aggregator hosts (yelp, tripadvisor, maps, ...) are
# engine.listing_hosts — host_blocked checks both.
HOST_BLOCKLIST_SUBSTRINGS = [
    "instagram.com/explore", "linkedin.com/pub", "youtube.com/results",
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
    # Visitor and travel guides are editorial pages: own_site.ARTICLE_PATH.
    "/map/", "/maps/",
    "/brochure", "/brochures",
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


# A community group's or non-profit's own site: how people take part.
MEMBER_FACING_MARKERS = [
    "join", "membership", "members", "volunteer", "donate", "our mission", "meetings",
    "meet ", "get involved", "contact us", "about us", "events",
]


_GENERIC_TITLE_PARTS = {"home", "homepage", "home page", "welcome", "official site",
                        "official website", "index"}
_TITLE_SPLIT = re.compile(r"\s+[|\-–—:]\s+")


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


def is_gov_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host.endswith(sfx) or (sfx in host) for sfx in GOV_HOST_SUFFIXES)


def host_blocked(url: str, allow_gov: bool = False) -> bool:
    host = urlparse(url).netloc.lower()
    if any(bad in host for bad in HOST_BLOCKLIST_SUBSTRINGS) or listing_hosts.is_listing_url(url):
        return True
    if not allow_gov and is_gov_host(url):
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
        return "directory utility/map/brochure URL"
    if own_site.is_article_page(link):
        return "an article or guide page"
    generic_hits = sum(1 for seg in segments if seg in DIRECTORY_SECTION_PATH_TOKENS)
    if generic_hits >= 2:
        return "generic directory/resort section path"
    return None


def name_from_title(title: Optional[str]) -> Optional[str]:
    """An organisation name from a page title: 'Home | Alibi Ale Works' →
    'Alibi Ale Works'. The first segment that isn't a generic page label."""
    for part in _TITLE_SPLIT.split(title or ""):
        part = part.strip()
        if part and part.lower() not in _GENERIC_TITLE_PARTS:
            return part[:120]
    return None


def not_own_site_reason(url: str, name: Optional[str], *, allow_gov: bool = False) -> Optional[str]:
    """Why the candidate `url` can't be `name`'s own site (engine/own_site;
    a homepage counts — the sweep found it as a site of its own), or None.
    A site utility page (jobs, member application, deals) never is. With
    `allow_gov` (community discovery), a page on a government host is that
    government's own site."""
    if own_site.is_utility_page(url):
        return "not its own site: a site utility page"
    if allow_gov and is_gov_host(url):
        return None
    why = own_site.not_own_reason(name, url, homepage_counts=True)
    return f"not its own site: {why}" if why else None


# "12 Best …", "The 25 Best …", "All 196+ …" — the words after the count.
_COUNT_OPENING = re.compile(r"^(?:the\s+|all\s+|our\s+)?\d+\+?\s+(.+)", re.IGNORECASE)
_COUNT_ROUNDUP_WORD = re.compile(r"(?:best|top|great(?:est)?|favou?rite|must)\b", re.IGNORECASE)
# Where a count's noun phrase ends: "12 Galleries | Worth a Visit", "15 Things | To Do".
_PHRASE_STOP = re.compile(r"\s(?:in|near|around|to|for|of|on|at|worth|that|you|we)\b.*",
                          re.IGNORECASE)
_PLACE = re.compile(r"\b(?:in|near|around)\b", re.IGNORECASE)
_ROUNDUP_PHRASES = [
    (re.compile(r"\bthings\s+to\s+do\b", re.IGNORECASE), "'things to do'"),
    (re.compile(r"\bbucket\s+list\b", re.IGNORECASE), "'bucket list'"),
    (re.compile(r"\bplaces\s+to\s+visit\b", re.IGNORECASE), "'places to visit'"),
    (re.compile(r"\bcan[’']?t[\s-]+miss\b", re.IGNORECASE), "'can't-miss'"),
    # "A Local's Guide to …", "Your Guide to …", "The Ultimate Guide" — not
    # "Mike's Guide Service" or "Tahoe Guide to Fishing Charters".
    (re.compile(r"[’']s\s+guide\b(?!\s+(?:service|services|company|co)\b)"
                r"|^(?:(?:a|an|the|your|our)\s+)?guide\s+to\b"
                r"|\b(?:ultimate|complete|travel|insider)\s+guide\b", re.IGNORECASE),
     "a 'guide' to somewhere"),
]
# "Best Restaurants …", "Top Restaurants in Tahoe" — but "Top of the Tram",
# "Best Western Station House Inn", "Tahoe's Best Pizza" are names.
_BEST_TOP = re.compile(r"\b(best|top)\b(?!\s+of\b)", re.IGNORECASE)


def _has_plural(text: str) -> bool:
    """A plural word ("Galleries", "Restaurants") — not "Glass", "Tahoe's"."""
    return any(len(w) >= 4 and w.endswith("s") and not w.endswith(("ss", "us", "is"))
               for w in re.findall(r"[a-z]+(?![’'])", text.lower()))


def _count_signal(text: str) -> Optional[str]:
    """A count before a roundup word or a plural: "12 Best …", "All 196+ South
    Lake Tahoe Restaurants" — not "3 Sisters Bakery" or "7 Eleven"."""
    if not (m := _COUNT_OPENING.match(text)):
        return None
    rest = m.group(1)
    head = _PHRASE_STOP.sub("", rest).split()
    if _COUNT_ROUNDUP_WORD.match(rest) or (head and _has_plural(head[-1])):
        return "opens with a count"
    return None


def _phrase_signal(text: str) -> Optional[str]:
    for rx, label in _ROUNDUP_PHRASES:
        if rx.search(text):
            return f"carries {label}"
    if (m := _BEST_TOP.search(text)) and (_has_plural(text) or _PLACE.search(text)):
        return f"carries '{m.group(1).lower()}'"
    return None


def _roundup_signal(url: str, title: Optional[str], from_search: bool) -> Optional[str]:
    path = urlparse(url).path or "/"
    if m := own_site.ARTICLE_PATH.search(path):
        return f"path /{m.group(1).lower()}/"
    for seg in filter(None, path.split("/")):
        words = " ".join(filter(None, re.split(r"[-_.]+", seg)))
        if why := _count_signal(words) or _phrase_signal(words):
            return f"slug {seg[:60]!r} {why}"
    homepage = own_site.is_homepage(url)
    for part in _TITLE_SPLIT.split((title or "").strip()):
        part = part.strip()
        if not part or own_site.host_carries_name(part, url):
            continue
        # A homepage's title is the organisation's name, whatever words it
        # carries ("The Best of Tahoe") — but no organisation is named "The
        # 10 Best Things To Do in Tahoe".
        if why := _count_signal(part) or (None if homepage else _phrase_signal(part)):
            return f"title {why} ({part[:60]!r})"
    # A search engine cut the title short: the name came from a results
    # page, not from the organisation. A page's own <title> may end "...".
    if from_search and (title or "").rstrip().endswith(("…", "...")):
        return "truncated search-result title"
    return None


def roundup_reason(url: str, title: Optional[str], *, from_search: bool = False) -> Optional[str]:
    """Why `url` is an article or roundup *about* organisations rather than
    one organisation's page, or None: an editorial path (own_site.ARTICLE_PATH);
    roundup words in its slug or `title` — a count before a roundup word or a
    plural, a roundup phrase (not on a homepage: its title is the
    organisation's name); or, `from_search` (`title` is the search-result
    name), a title cut short. A title segment its host carries
    (thebestoftahoe.com/about, "About Us | The Best of Tahoe") is the
    organisation's name and isn't read."""
    why = _roundup_signal(url, title, from_search)
    return f"an article or roundup: {why}" if why else None


# --------------------------------------------------------------------------
# Firecrawl client
# --------------------------------------------------------------------------

def _firecrawl_base() -> str:
    return firecrawl_env.base_url()


def _firecrawl_headers() -> dict:
    firecrawl_env.base_url()   # local instance configured, or raise — never cloud
    return firecrawl_env.headers()


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
    kind: str = "business",
    name: Optional[str] = None,
) -> dict:
    """Adversarial verify: scrape the candidate URL, decide whether it's a
    real customer-facing entity in the target town (member-facing, for a
    community group or non-profit `kind`) — on its own site, not a page
    about it (`not_own_site_reason`, judged with `name`, else the name the
    page title gives; a named candidate that fails is rejected unscraped).

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
    # Government departments are community discovery's, never a business.
    allow_gov = kind != "business"
    # Decided from the URL and the search-result name: no Firecrawl spent.
    unscraped = []
    if why := roundup_reason(canonical_url, name, from_search=True):
        unscraped.append(why)
    if name and (why := not_own_site_reason(canonical_url, name, allow_gov=allow_gov)):
        unscraped.append(why)
    if unscraped:
        return {"verified": False, "reasons": unscraped, "fetched_at": _iso_now()}
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

    if not name and (why := not_own_site_reason(
            canonical_url, name_from_title(scraped.get("title")), allow_gov=allow_gov)):
        reasons_reject.append(why)
    # The page's own title, whole: a name shortened from it ("Inside the
    # Upgrade | 24 BEST …" → "Inside the Upgrade") can hide the roundup.
    if why := roundup_reason(canonical_url, scraped.get("title")):
        reasons_reject.append(why)

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

    if kind == "business":
        customer_facing = any(marker in md_lower for marker in CUSTOMER_FACING_MARKERS)
        if not customer_facing:
            reasons_reject.append("no customer-facing markers (menu/hours/book/shop/services)")
    elif not any(marker in md_lower for marker in MEMBER_FACING_MARKERS):
        reasons_reject.append("no member-facing markers (join/volunteer/donate/meetings)")

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
        "reasons": ["in-town, no closure marker, "
                    + ("customer-facing" if kind == "business" else "member-facing")],
        "evidence": evidence,
        "markdown_head": md[:500],
        "title": scraped.get("title") or "",
        "address": address,
        "phone": phone,
        "hours": hours,
        "fetched_at": _iso_now(),
    }
