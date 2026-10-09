"""Find a record already on TownLoops before creating one.

Local staged rows drift from live: an event may already have been created
(a retry, an earlier run, the CMO, an operator) under our slug or another
one. So a create is only sent after a lookup comes back empty, and a 409 on
create means "look again", not "fine".

The event list has no `source_url` filter, so an event is looked up by slug
(GET); then, when the Series it will join is known, by that Series'
occurrences on the same town-local date (`?series=` + the day — the
strongest "already live" signal: a second source's copy of one evening is
the same occurrence, whatever its source_url or start time); then by
listing the events that start within a day of it and matching `source_url`. A recurring series shares one source_url across
instances, hence the time window and the same-start preference. Two or more
live rows for one event is a duplicate for the operator, never a guess.
See CMO ref `staged-events-already-live-and-duplicate-checks.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo
from typing import Optional

from engine.derive_helpers import parse_instant as _instant, utc_z as _utc_z


_WINDOW = timedelta(hours=12)


@dataclass
class LiveEventMatch:
    row: Optional[dict] = None                 # the one live event, when found
    ambiguous: list[dict] = field(default_factory=list)   # >1 candidates
    matched_by: Optional[str] = None           # slug | series-day | source_url


def is_not_found(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 404


def is_conflict(exc: Exception) -> bool:
    status = getattr(exc, "status", None)
    if status is not None:
        return status == 409
    msg = str(exc)
    return "409" in msg or "slug_taken" in msg


def find_live_entity(client, resource: str, slug: Optional[str]) -> Optional[dict]:
    """The live row at `slug`, or None on 404. Other API errors raise."""
    if not slug:
        return None
    try:
        return client.get(resource, slug) or None
    except Exception as e:
        if is_not_found(e):
            return None
        raise


def find_live_event(client, *, slug: Optional[str], source_url: Optional[str],
                    start_at: Optional[str], series_slug: Optional[str] = None,
                    tz: Optional[tzinfo] = None) -> LiveEventMatch:
    """Live event by `slug`; else, given the Series it will join and the town
    timezone, that Series' occurrence on the same town-local date; else by
    `source_url` among events starting within 12h of `start_at` (UTC with
    offset). Two or more hits at a step are ambiguous. API errors other than
    a 404 raise — the caller must not create on an unanswered lookup."""
    row = find_live_entity(client, "events", slug)
    if row:
        return LiveEventMatch(row=row, matched_by="slug")
    start = _instant(start_at)
    if series_slug and start and tz is not None:
        day = start.astimezone(tz).date()
        lo = datetime.combine(day, time.min, tzinfo=tz)
        rows = list(events_between(client, lo, lo + timedelta(days=1) - timedelta(seconds=1),
                                    series=series_slug))
        if len(rows) == 1:
            return LiveEventMatch(row=rows[0], matched_by="series-day")
        if rows:
            return LiveEventMatch(ambiguous=rows, matched_by="series-day")
    url = _norm_url(source_url)
    if not (start and url):
        return LiveEventMatch()
    rows = [r for r in events_between(client, start - _WINDOW, start + _WINDOW)
            if _norm_url(r.get("source_url")) == url]
    if len(rows) > 1:
        same = [r for r in rows if _instant(r.get("start_at")) == start]
        if len(same) == 1:
            rows = same
    if len(rows) == 1:
        return LiveEventMatch(row=rows[0], matched_by="source_url")
    return LiveEventMatch(ambiguous=rows, matched_by="source_url" if rows else None)


def events_between(client, lo: datetime, hi: datetime, page: int = 200, **filters):
    """Every live event starting in [lo, hi] — paged, since a busy window
    can exceed the API's 200-row limit."""
    params = {"from": _utc_z(lo), "to": _utc_z(hi), "limit": page, **filters}
    offset = 0
    while True:
        rows = client.list("events", **params, offset=offset) or []
        yield from rows
        if len(rows) < page:
            return
        offset += page


def _norm_url(url: Optional[str]) -> str:
    return (url or "").strip().rstrip("/")
