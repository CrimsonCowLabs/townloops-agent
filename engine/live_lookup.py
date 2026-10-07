"""Find a record already on TownLoops before creating one.

Local staged rows drift from live: an event may already have been created
(a retry, an earlier run, the CMO, an operator) under our slug or another
one. So a create is only sent after a lookup comes back empty, and a 409 on
create means "look again", not "fine".

The event list has no `source_url` filter, so an event is looked up by slug
(GET), then by listing the events that start within a day of it and
matching `source_url`. A recurring series shares one source_url across
instances, hence the time window and the same-start preference. Two or more
live rows for one event is a duplicate for the operator, never a guess.
See CMO ref `staged-events-already-live-and-duplicate-checks.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


_WINDOW = timedelta(hours=12)


@dataclass
class LiveEventMatch:
    row: Optional[dict] = None                 # the one live event, when found
    ambiguous: list[dict] = field(default_factory=list)   # >1 candidates


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
                    start_at: Optional[str]) -> LiveEventMatch:
    """Live event by `slug`, else by `source_url` among events starting
    within 12h of `start_at` (UTC with offset). API errors other than a 404
    raise — the caller must not create on an unanswered lookup."""
    row = find_live_entity(client, "events", slug)
    if row:
        return LiveEventMatch(row=row)
    start = _instant(start_at)
    url = _norm_url(source_url)
    if not (start and url):
        return LiveEventMatch()
    rows = [r for r in _events_between(client, start - _WINDOW, start + _WINDOW)
            if _norm_url(r.get("source_url")) == url]
    if len(rows) > 1:
        same = [r for r in rows if _instant(r.get("start_at")) == start]
        if len(same) == 1:
            rows = same
    if len(rows) == 1:
        return LiveEventMatch(row=rows[0])
    return LiveEventMatch(ambiguous=rows)


def _events_between(client, lo: datetime, hi: datetime, page: int = 200):
    """Every live event starting in [lo, hi] — paged, since a busy window
    can exceed the API's 200-row limit."""
    params = {"from": _utc_z(lo), "to": _utc_z(hi), "limit": page}
    offset = 0
    while True:
        rows = client.list("events", **params, offset=offset) or []
        yield from rows
        if len(rows) < page:
            return
        offset += page


def _norm_url(url: Optional[str]) -> str:
    return (url or "").strip().rstrip("/")


def _instant(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def _utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
