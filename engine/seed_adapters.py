"""Seed adapters: a deterministic reader per seed, chosen by the seed record.

`event_seeds.adapter` names how event-find reads a seed — NULL/'generic' is
the Firecrawl markdown → LLM path (event-find's own); any other key is one of
`ADAPTERS`, configured by `event_seeds.adapter_config` (a JSON object). No
URL or hostname conditionals: a seed is moved onto an adapter by setting its
key (`event-seed-discovery set-adapter`).

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
from typing import Callable, Optional
from urllib.parse import urljoin

import requests
from dateutil import tz as _tz

from engine import blocked_page
from engine.event_extract import town_tz_name
from engine.research import UA
from engine.feed_probe import TRIBE_REST_PATH

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
    events = adapter(seed, _config(seed), get_json or _get_json)
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


def _tribe_event(row: dict) -> dict:
    """A row without a title or start stays in (as None): event-find counts
    it as malformed rather than it vanishing here."""
    venue = row.get("venue") if isinstance(row.get("venue"), dict) else {}
    zip_part = " ".join(x for x in (venue.get("state"), venue.get("zip")) if x)
    location = ", ".join(x for x in (venue.get("address"), venue.get("city"), zip_part) if x)
    return {
        "title": _text(row.get("title")),
        "date": _local(row.get("utc_start_date"), row.get("start_date")),
        "end_at": _local(row.get("utc_end_date"), row.get("end_date")),
        "venue": _text(venue.get("venue")),
        "location": _html.unescape(location) or None,
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


ADAPTERS: dict[str, Callable[[dict, dict, GetJson], list[dict]]] = {
    "tribe": tribe,
}
