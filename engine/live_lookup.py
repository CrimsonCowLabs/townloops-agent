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

A record a curator merged into another 404s naming its survivor
(`merged_into_slug` in the error details, townloops ADR 0009). Reads by slug
follow it (`get_live`, built on `follow_merges`, at most MAX_MERGE_HOPS): the
survivor is the record, and the caller re-points its local row at the
survivor's `slug` (entities: `entity_sync.repoint`). Only a 404 without a
survivor means "gone". Only entity 404s name one — an event's never does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo
from typing import Callable, Optional, TypeVar

from engine.derive_helpers import parse_instant as _instant, utc_z as _utc_z


_WINDOW = timedelta(hours=12)
MAX_MERGE_HOPS = 3


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


def merged_into(details) -> Optional[str]:
    """The survivor named in a 404's error `details`, else None."""
    slug = details.get("merged_into_slug") if isinstance(details, dict) else None
    return slug if isinstance(slug, str) and slug else None


def merged_into_slug(exc: Exception) -> Optional[str]:
    """The survivor a merged-away entity's 404 (the client's error) names."""
    return merged_into(getattr(exc, "details", None)) if is_not_found(exc) else None


def error_details(resp) -> Optional[dict]:
    """`error.details` of a raw (requests) API error response, if any."""
    try:
        err = (resp.json() or {}).get("error") if resp.content else None
    except ValueError:
        return None
    return err.get("details") if isinstance(err, dict) else None


T = TypeVar("T")


def follow_merges(attempt: Callable[[str], tuple[T, Optional[str]]], slug: str,
                  on_survivor: Optional[Callable[[str], None]] = None,
                  ) -> tuple[T, str, Optional[str]]:
    """Run `attempt(slug)` → (result, survivor named, if merged); while it
    names a survivor, call `on_survivor(survivor)` (e.g. re-point the local
    row) and try again there — at most MAX_MERGE_HOPS hops, never back to a
    slug already tried. Returns (last result, last slug tried, the survivor
    left unfollowed — None when the chain resolved)."""
    seen = [slug]
    while True:
        result, survivor = attempt(slug)
        if not survivor:
            return result, slug, None
        if survivor in seen or len(seen) > MAX_MERGE_HOPS:
            return result, slug, survivor
        if on_survivor:
            on_survivor(survivor)
        seen.append(survivor)
        slug = survivor


def get_live(client, resource: str, slug: str) -> dict:
    """GET the live row at `slug`, following merges to the survivor
    (`follow_merges`). The row's own `slug` says where it was found. A 404
    without a survivor (or with one left unfollowed) and other API errors
    raise."""
    def attempt(s: str):
        try:
            return client.get(resource, s) or {}, None
        except Exception as e:
            if not merged_into_slug(e):
                raise
            return e, merged_into_slug(e)
    result, _, _ = follow_merges(attempt, slug)
    if isinstance(result, Exception):
        raise result
    return result


def find_live_entity(client, resource: str, slug: Optional[str]) -> Optional[dict]:
    """The live row at `slug` — or its merge survivor (`get_live`) — or None
    on 404. Other API errors raise."""
    if not slug:
        return None
    try:
        return get_live(client, resource, slug) or None
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
