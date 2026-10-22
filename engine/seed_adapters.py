"""Seed adapters: a deterministic reader per seed, chosen by the seed record.

`event_seeds.adapter` names how event-find reads a seed — NULL/'generic' is
the Firecrawl markdown → LLM path (event-find's own); any other key is one of
`ADAPTERS`, configured by `event_seeds.adapter_config` (a JSON object). No
URL or hostname conditionals: a seed is moved onto an adapter by setting its
key (`event-seed-discovery set-adapter`, or the autonomous `review` when an
adapter reads the seed's probed `feed_type`). Each `ADAPTERS` entry is an
`Adapter`: its reader, the settings its config may hold (`check_config`
refuses any other key or a bad value before anything is stored) and the
feed types it reads (`for_feed`).

`read(seed)` returns a `Listing`: events normalized to what the extraction
prompt returns (title, local-wall-clock `date`/`end_at`, venue, location,
description, categories, tags, source_url) — so they go through event-find's
one upsert / cancellation / vanish / source-health / judgement path — plus
`page`, the text those quote-check against. Anything short of the whole
listing raises `AdapterError`: a partial read would count every unread event
as missing, and a failed read must be a failed run, never a zero.
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests
from dateutil import tz as _tz

from engine import blocked_page
from engine.derive_helpers import is_http_url
from engine.event_extract import town_tz_name
from engine.research import UA
from engine.feed_probe import SHOWARE_LIST_PATH, TRIBE_REST_PATH

GENERIC = "generic"
TIMEOUT_S = 30

GetJson = Callable[[str, dict], dict]


class AdapterError(Exception):
    """The adapter could not read the seed's whole listing."""


@dataclass(frozen=True)
class Listing:
    events: list[dict]
    page: str


def adapter_of(seed: dict) -> str:
    return seed.get("adapter") or GENERIC


def read(seed: dict, get_json: Optional[GetJson] = None) -> Listing:
    key = adapter_of(seed)
    adapter = ADAPTERS.get(key)
    if adapter is None:
        raise AdapterError(f"unknown adapter {key!r}")
    events = adapter.read(seed, _config(seed), get_json or _get_json)
    return Listing(events, "\n\n".join(_page_entry(e) for e in events))


def _config(seed: dict) -> dict:
    raw = seed.get("adapter_config")
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise AdapterError(f"adapter_config is not JSON: {e}") from None
    if not isinstance(cfg, dict):
        raise AdapterError("adapter_config is not a JSON object")
    return cfg


def _get_json(url: str, params: dict) -> dict:
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT_S,
                         headers={"Accept": "application/json", "User-Agent": UA})
    except requests.RequestException as e:
        raise AdapterError(f"{type(e).__name__}: {e}") from None
    if r.ok:
        try:
            return r.json()
        except ValueError:
            pass
    # An error status, or a challenge / `no action` answered 200, is named.
    raise AdapterError(blocked_page.reason(r.text, r.status_code) or "response is not JSON")


def _page_entry(e: dict) -> str:
    where = ", ".join(x for x in (e.get("venue"), e.get("location")) if x)
    return "\n".join(x for x in (f"{e.get('title') or ''} — {e.get('date') or ''}", where,
                                 e.get("description"), e.get("source_url")) if x)


_BREAK_RE = re.compile(r"<br\s*/?>|</(?:p|div|li|h\d)>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(value) -> Optional[str]:
    """HTML → plain text: one line per paragraph, entities decoded."""
    if not isinstance(value, str):
        return None
    text = _html.unescape(_TAG_RE.sub("", _BREAK_RE.sub("\n", value)))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line) or None


def _local(utc_value, local_value) -> Optional[str]:
    """A feed time as the extraction's form: town-local wall clock, no offset."""
    if isinstance(utc_value, str) and utc_value.strip():
        try:
            dt = datetime.fromisoformat(utc_value.strip().replace(" ", "T"))
        except ValueError:
            dt = None
        if dt is not None:
            return (dt.replace(tzinfo=timezone.utc).astimezone(_tz.gettz(town_tz_name()))
                    .replace(tzinfo=None).isoformat(timespec="seconds"))
    if isinstance(local_value, str) and local_value.strip():
        return local_value.strip().replace(" ", "T")
    return None


# ---------------------------------------------------------------------------
# WordPress The Events Calendar REST (Tribe)
# ---------------------------------------------------------------------------

TRIBE_PER_PAGE = 50          # the API caps per_page (50 seen), whatever is asked
TRIBE_MAX_PAGES = 100


def _tribe_endpoint(seed: dict, config: dict) -> str:
    """config `url`, else the feed event-seed-discovery probed
    (engine/feed_probe), else the site's default WordPress REST root."""
    if config.get("url"):
        return config["url"]
    if seed.get("feed_type") == "tribe" and seed.get("feed_url"):
        return seed["feed_url"]
    return urljoin(seed["url"], "/wp-json/" + TRIBE_REST_PATH)


def _location(address, city, state, zip_code) -> Optional[str]:
    """"address, city, state zip" from whichever parts the feed gives."""
    zip_part = " ".join(x for x in (state, zip_code) if x)
    location = ", ".join(x for x in (address, city, zip_part) if x)
    return _html.unescape(location) or None


def _tribe_event(row: dict) -> dict:
    """A row without a title or start stays in (as None): event-find counts
    it as malformed rather than it vanishing here."""
    venue = row.get("venue") if isinstance(row.get("venue"), dict) else {}
    start = _local(row.get("utc_start_date"), row.get("start_date"))
    end = _local(row.get("utc_end_date"), row.get("end_date"))
    if row.get("all_day") is True and start:   # 00:00–23:59:59 is no published time
        start, end = start[:10], None
    return {
        "title": _text(row.get("title")),
        "date": start,
        "end_at": end,
        "venue": _text(venue.get("venue")),
        "location": _location(venue.get("address"), venue.get("city"), venue.get("state"),
                              venue.get("zip")),
        "description": _text(row.get("description")),
        "categories": [c["name"] for c in row.get("categories") or []
                       if isinstance(c, dict) and c.get("name")],
        "tags": [t["name"] for t in row.get("tags") or [] if isinstance(t, dict) and t.get("name")],
        "source_url": row.get("url") or None,
    }


def tribe(seed: dict, config: dict, get_json: GetJson) -> list[dict]:
    """Every page of the feed (`total_pages`, as each answer gives it), from
    today — through `days_ahead` when configured. A recurring event's
    occurrences are separate rows; one repeated across a page boundary is
    kept once (event id + start)."""
    url = _tribe_endpoint(seed, config)
    max_pages = int(config.get("max_pages") or TRIBE_MAX_PAGES)
    today = datetime.now(_tz.gettz(town_tz_name())).date()
    params = {"per_page": TRIBE_PER_PAGE, "start_date": today.isoformat()}
    if config.get("days_ahead"):
        params["end_date"] = (today + timedelta(days=int(config["days_ahead"]))).isoformat()
    events: list[dict] = []
    seen: set = set()
    page, total_pages = 1, 1
    while page <= total_pages:
        try:
            body = get_json(url, {**params, "page": page})
        except AdapterError as e:
            raise AdapterError(f"page {page}: {e}") from None
        rows = body.get("events") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise AdapterError(f"page {page}: answer has no events list")
        # Every answer's count: a feed that grows while it is paged is read to its end.
        total_pages = body.get("total_pages")
        if not isinstance(total_pages, int):
            raise AdapterError(f"page {page}: answer has no total_pages")
        if total_pages > max_pages:
            raise AdapterError(f"{total_pages} pages, more than max_pages {max_pages}")
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (row.get("id") or row.get("url"), row.get("start_date"))
            if key in seen:
                continue
            seen.add(key)
            events.append(_tribe_event(row))
        page += 1
    return events


# ---------------------------------------------------------------------------
# Showare box office performance list
# ---------------------------------------------------------------------------

SHOWARE_PAGE_SIZE = 100
SHOWARE_MAX_PERFORMANCES = 1000
_SHOWARE_TIME = "%A, %B %d, %Y %I:%M:%S %p"      # "Thursday, October 22, 2026 7:30:00 PM"


def _showare_endpoint(seed: dict, config: dict) -> tuple[str, dict]:
    """config `url`, else the feed event-seed-discovery probed
    (engine/feed_probe), else the seed's own box office — split from its
    query, whose settings are kept and paging replaced (a repeated `page`
    reads as "1, 2")."""
    if config.get("url"):
        url = config["url"]
    elif seed.get("feed_type") == "showare" and seed.get("feed_url"):
        url = seed["feed_url"]
    elif urlsplit(seed["url"]).path.lower() == SHOWARE_LIST_PATH:
        url = seed["url"]
    else:
        url = urljoin(seed["url"], SHOWARE_LIST_PATH)
    parts = urlsplit(url)
    return parts._replace(query="", fragment="").geturl(), dict(parse_qsl(parts.query))


def _showare_time(value) -> Optional[str]:
    """A Showare time as the extraction's form. It is the box office's wall
    clock: the `TimeZone`/`VenuetGMTOffset` beside it stay "PST"/-8 all year."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(" ".join(value.split()), _SHOWARE_TIME).isoformat(
            timespec="seconds")
    except ValueError:
        return None


def _undoubled(text: str) -> str:
    """The feed's address line can be its two parts, both the same: kept once."""
    words = text.split()
    half = len(words) // 2
    return " ".join(words[:half]) if half and words[:half] == words[half:] else " ".join(words)


def _showare_event(row: dict, box_office: str, shows_end: bool) -> dict:
    """A row without a title or start stays in (as None): event-find counts
    it as malformed rather than it vanishing here."""
    start = _showare_time(row.get("PerformanceDateTime"))
    end = _showare_time(row.get("PerformanceEndDateTime")) if shows_end else None
    if (row.get("PerformanceStartTimeAlias") or "").strip() and start:
        start, end = start[:10], None       # "TBA" in place of the clock: no published time
    event_id = row.get("EventID")
    # An event the box office sends elsewhere (`RedirectionEnabled`) is read there.
    redirect = row.get("RedirectURL") if row.get("RedirectionEnabled") else None
    if is_http_url(redirect):
        source_url = redirect
    elif event_id is not None:
        source_url = f"{box_office}/eventperformances.asp?evt={event_id}"
    else:
        source_url = None
    return {
        "title": _text(row.get("Event")),
        "date": start,
        "end_at": end,
        "venue": _text(row.get("Venue")),
        "location": _location(_undoubled(row.get("VenueAddress") or ""), row.get("VenueCity"),
                              row.get("VenueState"), row.get("VenueZipCode")),
        "description": _text(row.get("Description")),
        "categories": [],
        "tags": [],
        "source_url": source_url,
    }


def showare(seed: dict, config: dict, get_json: GetJson) -> list[dict]:
    """Every page of the performance list (`perfCount`, as each answer
    gives it, over SHOWARE_PAGE_SIZE rows a page). Each performance is its
    own occurrence (event id + performance id); fewer distinct performances
    than the count — a short page, a list that shifted while paged — is an
    error. The list holds upcoming performances only. The end time is kept
    only when the box office shows it (`settings.ShowEndTime`)."""
    url, query = _showare_endpoint(seed, config)
    max_perfs = int(config.get("max_performances") or SHOWARE_MAX_PERFORMANCES)
    parts = urlsplit(url)
    box_office = f"{parts.scheme}://{parts.netloc}"
    # `listMaxSize` caps `perfCount`: one over the cap tells a longer list apart.
    params = {**query, "action": query.get("action") or "perf",
              "listPageSize": SHOWARE_PAGE_SIZE, "listMaxSize": max_perfs + 1}
    events: list[dict] = []
    seen: set = set()
    page, total_pages, count = 1, 1, 0
    while page <= total_pages:
        try:
            body = get_json(url, {**params, "page": page})
        except AdapterError as e:
            raise AdapterError(f"page {page}: {e}") from None
        if not isinstance(body, dict):
            raise AdapterError(f"page {page}: answer has no performance list")
        count = body.get("perfCount")
        rows = [] if body.get("performance") is None and count == 0 else body.get("performance")
        if not isinstance(rows, list):
            raise AdapterError(f"page {page}: answer has no performance list")
        # Every answer's count: a list that grows while it is paged is read to its end.
        if not isinstance(count, int) or isinstance(count, bool):
            raise AdapterError(f"page {page}: answer has no perfCount")
        if count > max_perfs:
            raise AdapterError(f"more than max_performances {max_perfs} performances")
        settings = body.get("settings") if isinstance(body.get("settings"), dict) else {}
        shows_end = bool(settings.get("ShowEndTime"))
        total_pages = -(-count // SHOWARE_PAGE_SIZE)
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (row.get("EventID"), row.get("PerformanceID"))
            if key in seen:
                continue
            seen.add(key)
            events.append(_showare_event(row, box_office, shows_end))
        page += 1
    if len(seen) < count:
        raise AdapterError(f"read {len(seen)} of {count} performances")
    return events


# ---------------------------------------------------------------------------
# The registry: one entry per adapter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Param:
    help: str
    check: Callable[[Any], Optional[str]]    # value → what's wrong with it, None if fine


def _http_url(v) -> Optional[str]:
    return None if is_http_url(v) else "must be an http(s) URL"


def _positive_int(v) -> Optional[str]:
    ok = isinstance(v, int) and not isinstance(v, bool) and v > 0
    return None if ok else "must be a whole number above 0"


@dataclass(frozen=True)
class Adapter:
    """`read(seed, config, get_json)` → the whole listing's events;
    `params`, the settings its config may hold; `feed_types`, the probed
    `event_seeds.feed_type`s (engine/feed_probe) it reads as they are."""
    read: Callable[[dict, dict, GetJson], list[dict]]
    params: dict[str, Param]
    feed_types: tuple[str, ...] = ()


ADAPTERS: dict[str, Adapter] = {
    "tribe": Adapter(tribe, {
        "url": Param("the events endpoint (default: the probed feed, else /wp-json/…)", _http_url),
        "max_pages": Param(f"refuse a feed longer than this (default {TRIBE_MAX_PAGES})",
                           _positive_int),
        "days_ahead": Param("read events through this many days from today", _positive_int),
    }, feed_types=("tribe",)),
    "showare": Adapter(showare, {
        "url": Param("the performancelist.asp endpoint (default: the probed feed, else the "
                     "seed's box office)", _http_url),
        "max_performances": Param("refuse a list longer than this "
                                  f"(default {SHOWARE_MAX_PERFORMANCES})", _positive_int),
    }, feed_types=("showare",)),
}


def for_feed(feed_type: Optional[str]) -> Optional[str]:
    """The adapter that reads a seed's probed feed, None when none does."""
    return next((k for k, a in ADAPTERS.items() if feed_type and feed_type in a.feed_types),
                None)


def check_config(adapter: str, config: dict) -> None:
    """ValueError naming the first setting `adapter` doesn't declare, or
    whose value its check refuses."""
    params = ADAPTERS[adapter].params
    for key, value in config.items():
        if key not in params:
            raise ValueError(f"{adapter} has no setting {key!r} "
                             f"(settings: {', '.join(sorted(params)) or 'none'})")
        why = params[key].check(value)
        if why:
            raise ValueError(f"{adapter} setting {key!r} {why} (got {value!r})")
