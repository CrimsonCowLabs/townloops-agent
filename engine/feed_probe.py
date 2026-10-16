"""Feed probing: does a seed's page expose a machine-readable event feed?

`detect(html, page_url)` looks only at the page's own HTML (the Firecrawl
rawHtml event-seed-discovery already fetched to verify the seed) — no extra
requests, so probing costs no Firecrawl budget. It returns every feed it
finds, best first; `type`/`url` are the best one, both None when there's
nothing machine-readable.
"""

from __future__ import annotations

import html as _html
import json
import re
from typing import Optional
from urllib.parse import parse_qs, quote, urljoin, urlparse

from engine.ingest_core import walk_jsonld

# The Events Calendar's REST events route, under the site's WordPress REST
# root (engine/seed_adapters' `tribe` adapter reads it).
TRIBE_REST_PATH = "tribe/events/v1/events"

_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
# Calendar-view markup, not the plugin's CSS/JS (enqueued on every page).
_TRIBE_RE = re.compile(r"post-type-archive-tribe_events|tribe-events-calendar|tribe-events-c-"
                       r"|tribe-events-l-container|/wp-json/tribe/events", re.IGNORECASE)
_LDJSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL)
_EVENTISH_RE = re.compile(r"event|calendar", re.IGNORECASE)
_HTML_CALENDAR_MARKERS = ('class="event', "eventtitle", "event-list", "fc-event")
_URL_RE = re.compile(r"""https?://[^\s"'<>]+""", re.IGNORECASE)


def detect(html: str, page_url: str) -> dict:
    html = html or ""
    found: list[dict] = []
    for probe in _PROBES:
        found.extend(probe(html, page_url))
    # A note-only finding (the hidden Wix calendar) is best only when
    # there's no real feed.
    best = next((f for f in found if f["url"]), found[0] if found else {})
    return {"type": best.get("type"), "url": best.get("url"), "feeds": found}


def _attrs(tag: str) -> dict:
    return {m.group(1).lower(): _html.unescape(m.group(2) if m.group(2) is not None else m.group(3))
            for m in _ATTR_RE.finditer(tag)}


def _alternate_links(html: str, mime: str) -> list[dict]:
    out = []
    for tag in _LINK_RE.findall(html):
        a = _attrs(tag)
        if "alternate" in (a.get("rel") or "").lower().split() \
                and (a.get("type") or "").lower() == mime and a.get("href"):
            out.append(a)
    return out


def _urls(html: str, host_part: str) -> list[str]:
    """Absolute URLs in the page whose host contains `host_part`, in order,
    deduped, with HTML entities decoded."""
    out: list[str] = []
    for raw in _URL_RE.findall(html):
        url = _html.unescape(raw)
        if host_part in (urlparse(url).hostname or "") and url not in out:
            out.append(url)
    return out


def _feed(kind: str, url: Optional[str], **extra) -> dict:
    return {"type": kind, "url": url, **extra}


def _ical(html: str, page_url: str) -> list[dict]:
    return [_feed("ical", urljoin(page_url, a["href"]))
            for a in _alternate_links(html, "text/calendar")]


def _google_calendar(html: str, page_url: str) -> list[dict]:
    out = []
    for url in _urls(html, "calendar.google.com"):
        if "/embed" not in urlparse(url).path:
            continue
        for cal in parse_qs(urlparse(url).query).get("src", []):
            feed = ("https://calendar.google.com/calendar/ical/"
                    f"{quote(cal, safe='')}/public/basic.ics")
            if all(f["url"] != feed for f in out):
                out.append(_feed("google-calendar", feed))
    return out


def _tribe(html: str, page_url: str) -> list[dict]:
    # The Events Calendar (WordPress). Its REST feed paginates by the
    # response's `total_pages`, not the requested per_page.
    if not _TRIBE_RE.search(html):
        return []
    api_root = next((urljoin(page_url, a["href"]) for tag in _LINK_RE.findall(html)
                     for a in [_attrs(tag)]
                     if a.get("rel") == "https://api.w.org/" and a.get("href")),
                    urljoin(page_url, "/wp-json/"))
    return [_feed("tribe", api_root.rstrip("/") + "/" + TRIBE_REST_PATH)]


def _eventcalendarapp(html: str, page_url: str) -> list[dict]:
    for url in _urls(html, "eventcalendarapp.com"):
        q = parse_qs(urlparse(url).query)
        if q.get("id") and q.get("widgetUuid"):
            return [_feed("eventcalendarapp",
                          f"https://api.eventcalendarapp.com/events?id={q['id'][0]}"
                          f"&widgetUuid={q['widgetUuid'][0]}")]
    return []


def _point(html: str, page_url: str) -> list[dict]:
    # POINT volunteer/events iframes share one JSON feed keyed by the token;
    # basin-wide orgs need filtering to the town.
    for url in _urls(html, "pointapp"):
        token = parse_qs(urlparse(url).query).get("token")
        if token:
            return [_feed("point", "https://pointapp-8f268.appspot.com/api/v1/shared/"
                                   f"frames/events?token={quote(token[0], safe='')}")]
    return []


def _jsonld(html: str, page_url: str) -> list[dict]:
    count = 0
    for raw in _LDJSON_RE.findall(html):
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in walk_jsonld(blob):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            # Event and its schema.org subtypes (MusicEvent, SocialEvent, …)
            if any(isinstance(t, str) and t.endswith("Event") for t in types):
                count += 1
    return [_feed("jsonld", page_url, events=count)] if count else []


def _rss(html: str, page_url: str) -> list[dict]:
    # Only an events feed: most sites' RSS is the blog or comments.
    return [_feed("rss", urljoin(page_url, a["href"]))
            for mime in ("application/rss+xml", "application/atom+xml")
            for a in _alternate_links(html, mime)
            if _EVENTISH_RE.search(a.get("title", "") + " " + a["href"])]


def _html_calendar(html: str, page_url: str) -> list[dict]:
    low = html.lower()
    if any(m in low for m in _HTML_CALENDAR_MARKERS):
        return [_feed("html-calendar", page_url)]
    return []


def _wix_calendar_wrapper(html: str, page_url: str) -> list[dict]:
    # Wix pages put a Google Calendar in a filesusr.com HTML component that
    # only receives the calendar src by postMessage: the feed isn't in the
    # page, and the rendered widget isn't dated evidence on its own.
    wrappers = [u for u in _urls(html, "filesusr.com") if "/html/" in urlparse(u).path]
    if not wrappers:
        return []
    return [_feed("wix-calendar-wrapper", None, embed=wrappers[0],
                  note="Wix calendar widget; its Google Calendar feed is hidden — "
                       "find the calendar URL before extracting dated events")]


# Best first.
_PROBES = (_ical, _google_calendar, _tribe, _eventcalendarapp, _point, _jsonld,
           _rss, _html_calendar, _wix_calendar_wrapper)
