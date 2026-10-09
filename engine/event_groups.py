"""Collapse a listing of Events on their Series (issue #3, ADR 0007).

TownLoops Events are per-occurrence: a weekly trivia night is seven Events, a
daily music slot forty-four. Every listing the agent produces — the weekly
newsletter, the nightly digest, the upcoming audit, the operator chat — groups
them here, so collapsing behaves the same everywhere.

- The group key is `series_slug`, or the Event's own key for a singleton.
  Never the title: two venues can run an event under one name.
- A group is represented by its earliest occurrence in the listing; the rest
  are its other dates. Groups come back in their representatives' order.
- `series_slug` is a grouping key, not a URL. `SeriesDetails` reads
  `GET /series/{slug}` once per slug per run, and `hub_url` gives a link only
  for an active Series with a `url` — an archived Series' hub 404s.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlsplit

from engine.townloops_api import TownloopsError


@dataclass
class EventGroup:
    key: str
    series_slug: Optional[str]
    events: list[dict] = field(default_factory=list)   # earliest first

    @property
    def representative(self) -> dict:
        return self.events[0]

    @property
    def others(self) -> list[dict]:
        return self.events[1:]

    @property
    def is_series(self) -> bool:
        return self.series_slug is not None


def group_events(events: Iterable[dict], *,
                 own_key: Callable[[dict], str] = lambda e: e.get("slug") or "",
                 start: Callable[[dict], str] = lambda e: e.get("start_at") or "",
                 ) -> list[EventGroup]:
    """Ordered groups of `events`. `own_key` keys a singleton (the live slug
    by default; a local row passes its `event_uid`), `start` orders them."""
    groups: dict[str, EventGroup] = {}
    for e in sorted(events, key=lambda e: (start(e), own_key(e))):
        slug = e.get("series_slug") or None
        key = f"series:{slug}" if slug else f"event:{own_key(e)}"
        groups.setdefault(key, EventGroup(key, slug)).events.append(e)
    return list(groups.values())


def group_local_rows(rows: Iterable[dict]) -> list[EventGroup]:
    """`group_events` for local `events` rows selected with `event_uid`,
    `series_slug` (mirrored from TownLoops) and a `date_sort` column."""
    return group_events(rows, own_key=lambda r: r["event_uid"],
                        start=lambda r: r.get("date_sort") or "")


def split_counts(groups: Iterable[EventGroup]) -> dict:
    """{"series": n, "singletons": n} — the variety of a listing."""
    groups = list(groups)
    series = sum(g.is_series for g in groups)
    return {"series": series, "singletons": len(groups) - series}


class SeriesDetails:
    """`GET /series/{slug}`, cached for the run. An unreadable Series (404,
    another town's, an API error) is `None`: its group still collapses, it
    just has no cadence or link."""

    def __init__(self, client):
        self._client = client
        self._cache: dict[str, Optional[dict]] = {}

    def get(self, slug: str) -> Optional[dict]:
        if slug not in self._cache:
            try:
                self._cache[slug] = self._client.get("series", slug)
            except TownloopsError:
                self._cache[slug] = None
        return self._cache[slug]


def site_origin() -> str:
    """The public site's origin: `TOWNLOOPS_SITE_URL` when the site and API
    are on different hosts, else `TOWNLOOPS_API_URL`'s origin. Resources carry
    site-relative `url`s like `/slt/series/trivia-thursday`."""
    parts = urlsplit(os.environ.get("TOWNLOOPS_SITE_URL") or os.environ.get("TOWNLOOPS_API_URL", ""))
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def public_url(url: Optional[str]) -> Optional[str]:
    """A resource `url` made absolute against the site origin."""
    if not url:
        return None
    origin = site_origin()
    return urljoin(origin + "/", url) if origin else url


def hub_url(series: Optional[dict]) -> Optional[str]:
    """The Series hub link, only while the Series is active and has a `url`."""
    if not series or not series.get("is_active") or not series.get("url"):
        return None
    return public_url(series["url"])
