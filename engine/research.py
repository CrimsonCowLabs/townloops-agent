"""Web research helpers for entity enrichment.

Fork of ~/.hermes/skills/business/townloops-research/research.py:128-270 —
kind auto-detection + og:image capture + JSON-LD extraction. The CLI /
drafts-DB plumbing is intentionally omitted; consumers here are the
enrichment skills (venue/business/performer) that manage their own queues.

Firecrawl: the town's local instance only (engine.firecrawl_env —
TOWNLOOPS_FIRECRAWL_URL / FIRECRAWL_API_URL, never cloud). With none
configured, firecrawl_fetch returns (None, None) and consumers fall back to
plain requests via research_url()'s degrade path.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from engine import firecrawl_env
from engine.log import get_logger

log = get_logger("research")


UA = "townloops-agent/1.0 (+https://townloops.com)"


class FirecrawlAuthError(RuntimeError):
    """Raised on 401/403 — the API key is bad; retrying won't help. Fatal."""


# Per-process meter. Read via firecrawl_stats(); skills print it in their
# sweep summaries so operators see when the invoice is climbing.
_STATS = {"calls": 0, "ok": 0, "http_error": 0, "network_error": 0, "last_error": None}


def firecrawl_stats() -> dict:
    return dict(_STATS)


def _firecrawl_base() -> str:
    return firecrawl_env.base_url()


@dataclass
class ResearchResult:
    url: str
    kind: Optional[str] = None                 # venue|business|community|performer|creator
    confidence: float = 0.0                    # 0..1
    payload: dict[str, Any] = field(default_factory=dict)
    fetched_from: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    text: str = ""                             # the page's visible text


# -------- fetch layer --------

def fetch_html(url: str, timeout: float = 20.0) -> tuple[Optional[str], Optional[str]]:
    """Return (html, final_url) or (None, None) on failure."""
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
    except requests.RequestException:
        return None, None
    if r.status_code >= 400 or not r.text:
        return None, None
    return r.text, r.url


def firecrawl_fetch(url: str) -> tuple[Optional[str], Optional[str]]:
    """Firecrawl fallback for JS-heavy or bot-blocked pages.

    Returns (html, final_url) on success, (None, None) on failure.
    Increments the module-level meter regardless. Logs every non-2xx to
    stderr so a rotated/wrong API key doesn't silently drain the invoice.
    Raises FirecrawlAuthError on 401/403 — the caller should escalate,
    not silently degrade for the rest of the sweep.
    """
    try:
        endpoint = f"{_firecrawl_base()}/v1/scrape"
    except firecrawl_env.FirecrawlNotConfigured as exc:
        _STATS["http_error"] += 1
        _STATS["last_error"] = str(exc)
        return None, None
    # Shared QPS ceiling with engine.discovery's Firecrawl calls.
    from engine.policy import firecrawl_bucket
    try:
        firecrawl_bucket.wait()
    except TimeoutError as exc:
        _STATS["last_error"] = f"rate limited: {exc}"
        return None, None
    _STATS["calls"] += 1
    try:
        r = requests.post(
            endpoint,
            json={"url": url, "formats": ["html"]},
            headers=firecrawl_env.headers(),
            timeout=45,
        )
    except requests.RequestException as exc:
        _STATS["network_error"] += 1
        _STATS["last_error"] = f"{type(exc).__name__}: {exc}"
        log.warning("firecrawl network error",
                     extra={"endpoint": endpoint, "target": url,
                            "err_kind": type(exc).__name__, "err": str(exc)})
        return None, None
    if r.status_code in (401, 403):
        _STATS["http_error"] += 1
        _STATS["last_error"] = f"{r.status_code}: {r.text[:200]}"
        log.error("firecrawl auth rejected",
                   extra={"endpoint": endpoint, "http_status": r.status_code,
                          "body_sample": r.text[:200]})
        raise FirecrawlAuthError(
            f"Firecrawl rejected the API key (HTTP {r.status_code}): {r.text[:200]}"
        )
    if r.status_code >= 400:
        _STATS["http_error"] += 1
        _STATS["last_error"] = f"{r.status_code}: {r.text[:200]}"
        log.warning("firecrawl http error",
                     extra={"endpoint": endpoint, "target": url,
                            "http_status": r.status_code,
                            "body_sample": r.text[:200]})
        return None, None
    _STATS["ok"] += 1
    try:
        data = r.json().get("data", {})
    except ValueError:
        _STATS["http_error"] += 1
        _STATS["last_error"] = "non-JSON 2xx body"
        log.warning("firecrawl non-JSON 2xx",
                     extra={"endpoint": endpoint, "target": url})
        return None, None
    return data.get("html"), url


# -------- extractors --------

_OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:([^"\']+)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_META_NAME_RE = re.compile(
    r'<meta[^>]+name=["\']([^"\']+)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_LDJSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def page_text(html: str) -> str:
    """A page's visible text, one block per line."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in soup.get_text("\n").splitlines())
    return "\n".join(ln for ln in lines if ln)


def og_tags(html: str) -> dict[str, str]:
    return {k.lower(): v for k, v in _OG_RE.findall(html)}


def meta_tags(html: str) -> dict[str, str]:
    return {k.lower(): v for k, v in _META_NAME_RE.findall(html)}


def title_text(html: str) -> Optional[str]:
    m = _TITLE_RE.search(html)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def ld_json_blocks(html: str) -> list[Any]:
    out: list[Any] = []
    for raw in _LDJSON_RE.findall(html):
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def ld_types(blocks: list[Any]) -> list[str]:
    """Flatten @type values from all top-level and nested LD-JSON blocks."""
    types: list[str] = []
    def walk(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            t = node.get("@type")
            if isinstance(t, str):
                types.append(t)
            elif isinstance(t, list):
                types.extend(x for x in t if isinstance(x, str))
            for v in node.values():
                walk(v)
    walk(blocks)
    return types


# -------- kind auto-detection --------

# Ordered highest-confidence first.
_KIND_LDJSON = {
    "MusicGroup": ("performer", 0.95),
    "PerformingGroup": ("performer", 0.9),
    "MusicVenue": ("venue", 0.9),
    "EventVenue": ("venue", 0.9),
    "Restaurant": ("venue", 0.9),
    "Bar": ("venue", 0.9),
    "LocalBusiness": ("business", 0.85),
    "Store": ("business", 0.85),
    "Organization": ("business", 0.5),
    "NGO": ("community", 0.8),
    "Person": ("creator", 0.7),
    "MusicRecording": ("performer", 0.6),
}


def detect_kind(html: str, url: str) -> tuple[Optional[str], float, list[str]]:
    """Return (kind, confidence, notes). Prefers JSON-LD, falls back to og:type."""
    notes: list[str] = []
    blocks = ld_json_blocks(html)
    for t in ld_types(blocks):
        if t in _KIND_LDJSON:
            kind, conf = _KIND_LDJSON[t]
            notes.append(f"ld+json @type={t}")
            return kind, conf, notes
    og = og_tags(html)
    og_type = og.get("type", "")
    if og_type.startswith("music.musician"):
        return "performer", 0.8, ["og:type=music.musician"]
    if og_type in ("business.business", "place"):
        return "business", 0.6, [f"og:type={og_type}"]
    if og_type == "profile":
        return "creator", 0.5, ["og:type=profile"]
    # Domain heuristics.
    host = urlparse(url).hostname or ""
    if host.endswith("facebook.com") and "/events/" not in url:
        return "business", 0.35, ["facebook page fallback"]
    if host.endswith("bandcamp.com") or host.endswith("spotify.com"):
        return "performer", 0.7, [f"{host} fallback"]
    return None, 0.0, notes


# -------- image capture --------

# Logos, icons and stand-ins are worse than no image: a null image_path
# is a gap the operator sees, a generic one looks done.
# Whole words only ("icon" not "silicon"), except "logo", which turns up
# glued into names ("sitelogo.png", "/logos/").
_GENERIC_IMAGE_RE = re.compile(
    r"logo|placeholder|favicon|spacer|sprite|gravatar|"
    r"(?<![a-z])(?:icons?|default|blank|pixel|no[-_]?image|coming[-_]?soon|missing|"
    r"badges?|avatars?)(?![a-z])",
    re.IGNORECASE,
)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(?<![-\w])(src|alt)=["\']([^"\']*)["\']', re.IGNORECASE)


def is_generic_image(url: str, hint: str = "") -> bool:
    """True for logos, icons, placeholders, avatars, SVG/ICO/GIF files and
    data URIs — images that don't show the entity itself. `hint` is the
    <img> alt text."""
    p = urlparse(url)
    if url.startswith("data:") or p.path.lower().endswith((".svg", ".ico", ".gif")):
        return True
    return bool(_GENERIC_IMAGE_RE.search(f"{p.hostname or ''}{p.path}")
                or _GENERIC_IMAGE_RE.search(hint))


def image_candidates(html: str, base_url: str) -> list[str]:
    """Usable https image URLs in priority order: og:image → JSON-LD
    `image` (never `logo`) → content <img> (the hero is usually first).
    Logos and placeholders are dropped."""
    found: list[str] = []
    og = og_tags(html).get("image")
    if og:
        found.append(og)
    for block in ld_json_blocks(html):
        found.extend(_ld_images(block))
    for tag in _IMG_RE.findall(html):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        src = attrs.get("src")
        if src and not is_generic_image(src, attrs.get("alt", "")):
            found.append(src)
    out: list[str] = []
    for raw in found:
        url = urljoin(base_url, raw)
        if url.startswith("https://") and not is_generic_image(url) and url not in out:
            out.append(url)
    return out


def _ld_images(node: Any) -> list[str]:
    """Every JSON-LD `image` value (string, {url}, or a list of either)."""
    out: list[str] = []
    if isinstance(node, list):
        for child in node:
            out.extend(_ld_images(child))
    elif isinstance(node, dict):
        v = node.get("image")
        for item in v if isinstance(v, list) else [v]:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and isinstance(item.get("url"), str):
                out.append(item["url"])
        for key, child in node.items():
            if key != "image":
                out.extend(_ld_images(child))
    return out


def verify_image(url: str, timeout: float = 10.0) -> bool:
    try:
        r = requests.head(url, timeout=timeout, headers={"User-Agent": UA}, allow_redirects=True)
    except requests.RequestException:
        return False
    return r.status_code < 400 and (r.headers.get("content-type", "").startswith("image/"))


# -------- top-level research --------

def research_url(url: str, *, use_firecrawl: bool = False) -> ResearchResult:
    """Fetch and extract everything we can about the entity at `url`.

    Fills what we can; leaves unknowns as None. Callers merge into an existing
    row rather than overwrite.
    """
    result = ResearchResult(url=url)
    html, final_url = fetch_html(url)
    if not html and use_firecrawl:
        html, final_url = firecrawl_fetch(url)
        if html:
            result.notes.append("used firecrawl fallback")
    if not html:
        result.notes.append("fetch failed")
        return result
    result.fetched_from.append(final_url or url)
    result.text = page_text(html)

    og = og_tags(html)
    meta = meta_tags(html)
    kind, conf, kind_notes = detect_kind(html, final_url or url)
    result.kind = kind
    result.confidence = conf
    result.notes.extend(kind_notes)

    payload: dict[str, Any] = {}
    # Verbatim page text, entities decoded — never rewritten.
    payload["name"] = html_lib.unescape(og.get("site_name") or og.get("title") or title_text(html) or "")
    payload["description"] = html_lib.unescape(og.get("description") or meta.get("description") or "")
    payload["website"] = og.get("url") or final_url or url

    for image in image_candidates(html, final_url or url):
        if verify_image(image):
            payload["image_path"] = image
            payload["image_alt"] = payload.get("name")
            break

    # LD-JSON fills — address, phone, sameAs socials.
    for block in ld_json_blocks(html):
        _fill_from_ldjson(payload, block)

    # Strip nones for a clean merge.
    result.payload = {k: v for k, v in payload.items() if v}
    return result


# Maps a sameAs URL's hostname to the payload field name it fills. Keys are
# lowercased; both bare and www-prefixed variants are listed so we don't have
# to normalize on every lookup.
#
# Note the two YouTube keys: `youtube_url` is what the creator payload uses,
# `youtube` is what business/community/performer use. Callers write to the
# correct field per kind — this map is a superset containing both spellings.
# _fill_from_ldjson only writes the first-seen key, so we keep both here and
# let per-kind post-processing pick the right one when both spellings exist
# on the target row.
_SOCIAL_HOSTS = {
    "instagram.com": "instagram",
    "www.instagram.com": "instagram",
    "facebook.com": "facebook",
    "www.facebook.com": "facebook",
    "m.facebook.com": "facebook",
    "tiktok.com": "tiktok",
    "www.tiktok.com": "tiktok",
    "twitter.com": "twitter",
    "www.twitter.com": "twitter",
    "x.com": "twitter",
    "www.x.com": "twitter",
    "linkedin.com": "linkedin",
    "www.linkedin.com": "linkedin",
    "spotify.com": "spotify_url",
    "open.spotify.com": "spotify_url",
    # Bare `youtube`, per the current payload spec for business/community/performer.
    # Creator wants `youtube_url`; enrichment SPECS handle the split at fill-time.
    "youtube.com": "youtube",
    "www.youtube.com": "youtube",
    "youtu.be": "youtube",
}


def _fill_from_ldjson(payload: dict, node: Any) -> None:
    if isinstance(node, list):
        for n in node:
            _fill_from_ldjson(payload, n)
        return
    if not isinstance(node, dict):
        return
    if "telephone" in node and "phone" not in payload:
        payload["phone"] = node["telephone"]
    addr = node.get("address")
    if addr and "address" not in payload:
        if isinstance(addr, dict):
            payload["address"] = ", ".join(
                s for s in (
                    addr.get("streetAddress"),
                    addr.get("addressLocality"),
                    addr.get("addressRegion"),
                    addr.get("postalCode"),
                ) if s
            )
        elif isinstance(addr, str):
            payload["address"] = addr
    same_as = node.get("sameAs")
    if isinstance(same_as, str):
        same_as = [same_as]
    if isinstance(same_as, list):
        for link in same_as:
            host = (urlparse(link).hostname or "").lower()
            field_name = _SOCIAL_HOSTS.get(host)
            if field_name and field_name not in payload:
                payload[field_name] = link
    for v in node.values():
        _fill_from_ldjson(payload, v)
