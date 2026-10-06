"""Web research helpers for entity enrichment.

Fork of ~/.hermes/skills/business/townloops-research/research.py:128-270 —
kind auto-detection + og:image capture + JSON-LD extraction. The CLI /
drafts-DB plumbing is intentionally omitted; consumers here are the
enrichment skills (venue/business/performer) that manage their own queues.

Env:
  FIRECRAWL_API_KEY  cloud bearer (or self-hosted bearer if the instance
                     validates keys). Optional when FIRECRAWL_API_URL is set —
                     a placeholder bearer is sent so self-hosted requests
                     satisfy the schema.
  FIRECRAWL_API_URL  base URL (no /v1). Default: https://api.firecrawl.dev.
                     Set to e.g. http://localhost:8302 for self-hosted.
                     Also unblocks firecrawl_fetch when no API key is set.
  When both are missing, firecrawl_fetch returns (None, None) and consumers
  fall back to plain requests via research_url()'s degrade path.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests

from engine.firecrawl_env import self_hosted_url
from engine.log import get_logger

log = get_logger("research")


UA = "townloops-agent/1.0 (+https://townloops.com)"
FIRECRAWL_DEFAULT_BASE = "https://api.firecrawl.dev"


class FirecrawlAuthError(RuntimeError):
    """Raised on 401/403 — the API key is bad; retrying won't help. Fatal."""


# Per-process meter. Read via firecrawl_stats(); skills print it in their
# sweep summaries so operators see when the invoice is climbing.
_STATS = {"calls": 0, "ok": 0, "http_error": 0, "network_error": 0, "last_error": None}


def firecrawl_stats() -> dict:
    return dict(_STATS)


def _firecrawl_base() -> str:
    """Base URL. Honors $FIRECRAWL_API_URL for self-hosted; strips a trailing /v1
    since we always append /v1/... ourselves."""
    raw = (self_hosted_url() or FIRECRAWL_DEFAULT_BASE).rstrip("/")
    if raw.endswith("/v1"):
        raw = raw[:-3]
    return raw


@dataclass
class ResearchResult:
    url: str
    kind: Optional[str] = None                 # venue|business|community|performer|creator
    confidence: float = 0.0                    # 0..1
    payload: dict[str, Any] = field(default_factory=dict)
    fetched_from: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


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
    # 2026-07-10: honor self-hosted mode when FIRECRAWL_API_URL is set — the
    # docstring above always said "optional; falls back to plain requests",
    # but the old key check silently no-op'd for self-hosted deployments too.
    # Now: real key takes priority; otherwise, if URL is set (self-hosted),
    # send a placeholder bearer that satisfies Firecrawl's schema. Only both-
    # missing is a real "not configured" state.
    key = (os.environ.get("FIRECRAWL_API_KEY") or "").strip()
    self_hosted = bool(self_hosted_url())
    if not key and not self_hosted:
        _STATS["http_error"] += 1
        _STATS["last_error"] = "FIRECRAWL_API_KEY not set and no FIRECRAWL_API_URL"
        return None, None
    _STATS["calls"] += 1
    endpoint = f"{_firecrawl_base()}/v1/scrape"
    bearer = key or "self-hosted"
    try:
        r = requests.post(
            endpoint,
            json={"url": url, "formats": ["html"]},
            headers={"Authorization": f"Bearer {bearer}"},
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

def og_image(html: str, base_url: str) -> Optional[str]:
    """Preferred order: og:image → JSON-LD image → first content <img>."""
    og = og_tags(html)
    if og.get("image"):
        return urljoin(base_url, og["image"])
    for block in ld_json_blocks(html):
        img = _extract_ld_image(block)
        if img:
            return urljoin(base_url, img)
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    return urljoin(base_url, m.group(1)) if m else None


def _extract_ld_image(node: Any) -> Optional[str]:
    if isinstance(node, str) and node.startswith(("http", "/")):
        return node
    if isinstance(node, dict):
        for key in ("image", "logo"):
            v = node.get(key)
            if isinstance(v, str):
                return v
            if isinstance(v, dict) and "url" in v:
                return v["url"]
            if isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, str):
                    return first
                if isinstance(first, dict) and "url" in first:
                    return first["url"]
        for child in node.values():
            found = _extract_ld_image(child)
            if found:
                return found
    if isinstance(node, list):
        for child in node:
            found = _extract_ld_image(child)
            if found:
                return found
    return None


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

    og = og_tags(html)
    meta = meta_tags(html)
    kind, conf, kind_notes = detect_kind(html, final_url or url)
    result.kind = kind
    result.confidence = conf
    result.notes.extend(kind_notes)

    payload: dict[str, Any] = {}
    payload["name"] = og.get("site_name") or og.get("title") or title_text(html)
    payload["description"] = og.get("description") or meta.get("description")
    payload["website"] = og.get("url") or final_url or url

    image = og_image(html, final_url or url)
    if image and verify_image(image):
        payload["image_path"] = image
        payload["image_alt"] = payload.get("name")

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
