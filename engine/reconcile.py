"""Cross-reference freshly extracted seed events against live TownLoops.

Pure logic, no I/O — the event-reconcile skill fetches the live catalog and
the seed extractions, this module decides what each extraction *is*:

    unchanged  live event found, same local day/time and venue → nothing to do
    changed    live event found but its start time and/or venue differ
    new        no live counterpart → candidate for the operator queue

Matching order (first hit wins):
  1. `source_url` — canonicalized; the strongest key, most live rows have one.
  2. Title similarity (normalized SequenceMatcher ≥ TITLE_MATCH_RATIO).

Recurring events share a title and often a source_url across occurrences, so
a key match on a *different* day is not automatically a date change. Where
TownLoops has put the live row in a Series (`series_slug`, ADR 0007 — archived
Series included: it is a grouping key), that decides it: a new date in a
Series is a new occurrence, never a reschedule. Live rows with no Series fall
back to the rules below. `classify_all` runs two passes: first every
extraction that lands on a live occurrence the same local day claims it; then
a leftover extraction is a date change only if its key matches exactly one live
occurrence in total (a one-off: no Series, no siblings), that occurrence is unclaimed,
not already in the past, and inside DATE_CHANGE_WINDOW. Anything else is a new
occurrence — the live window reaches back a couple of days, so past series
occurrences are always unclaimed and must never be "rescheduled" forward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

from dateutil import parser as _dtparser

from engine.derive_helpers import names_match, normalize_name
from engine.discovery import canonicalize_url
from engine.event_extract import day_and_clock, has_clock_time, page_times


TITLE_MATCH_RATIO = 0.88
VENUE_MATCH_RATIO = 0.80
# A start-time shift smaller than this is treated as rounding noise between
# the source page and what was published (e.g. "doors 6:45 / show 7").
TIME_TOLERANCE = timedelta(minutes=30)
# How far a single unclaimed live occurrence may sit from the extracted date
# and still count as "the same event, rescheduled".
DATE_CHANGE_WINDOW = timedelta(days=60)


@dataclass
class Classification:
    kind: str                                  # unchanged | changed | new | skipped
    extracted: dict
    live: Optional[dict] = None
    changes: dict = field(default_factory=dict)
    matched_by: Optional[str] = None           # source_url | title
    reason: str = ""

    def summary(self) -> str:
        if self.kind != "changed":
            return self.kind
        parts = []
        if "start_at" in self.changes:
            c = self.changes["start_at"]
            parts.append(f"date {c['live_local']} → {c['source_local']}")
        if "venue" in self.changes:
            c = self.changes["venue"]
            parts.append(f"venue {c['live'] or '?'} → {c['source']}")
        return "; ".join(parts)


# --------------------------------------------------------------------------
# Parsing / normalizing
# --------------------------------------------------------------------------

def parse_start(value: Any, tz: tzinfo) -> tuple[Optional[datetime], bool]:
    """Parse an ISO-ish start. Returns (aware datetime, has_time).

    Naive values are interpreted in the town timezone — that is what the
    extraction prompt asks the LLM to emit. A value with no time of day
    (`event_extract.day_and_clock`: date-only, or town-local midnight) is
    flagged has_time=False so only the day is compared."""
    if not value or not isinstance(value, str):
        return None, False
    try:
        dt = _dtparser.isoparse(value.strip())
    except (ValueError, OverflowError):
        try:
            dt = _dtparser.parse(value.strip())
        except (ValueError, OverflowError):
            return None, False
    if day_and_clock(value):
        has_time = has_clock_time(value)
    else:   # not ISO: a time only where the text states one
        has_time = has_clock_time(dt) and (dt.hour, dt.minute) in page_times(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt, has_time


def _canon(url: Optional[str]) -> Optional[str]:
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    try:
        return canonicalize_url(url)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Live index
# --------------------------------------------------------------------------

@dataclass
class LiveIndex:
    events: list[dict]
    venues_by_slug: dict[str, dict]
    tz: tzinfo

    @classmethod
    def build(cls, live_events: Iterable[dict], live_venues: Iterable[dict],
              tz: tzinfo) -> "LiveIndex":
        evs = []
        for e in live_events:
            start, _ = parse_start(e.get("start_at"), tz)
            if start is None:
                continue
            e = dict(e)
            e["_start"] = start
            e["_title_norm"] = normalize_name(e.get("title") or "")
            e["_url"] = _canon(e.get("source_url"))
            evs.append(e)
        return cls(
            events=evs,
            venues_by_slug={v["slug"]: v for v in live_venues if v.get("slug")},
            tz=tz,
        )

    def venue_name_of(self, live_event: dict) -> Optional[str]:
        v = self.venues_by_slug.get(live_event.get("venue_slug") or "")
        if v:
            return v.get("name")
        return live_event.get("venue_name")

    def candidates(self, extracted: dict, seed_url: Optional[str]) -> tuple[list[dict], Optional[str]]:
        url = _canon(extracted.get("source_url"))
        # A source_url equal to the seed listing page identifies nothing —
        # the LLM falls back to it when an event has no detail link.
        if url and url != _canon(seed_url):
            hits = [e for e in self.events if e["_url"] == url]
            if hits:
                return hits, "source_url"
        t = normalize_name(extracted.get("title") or "")
        if not t:
            return [], None
        hits = [e for e in self.events
                if e["_title_norm"]
                and SequenceMatcher(None, t, e["_title_norm"]).ratio() >= TITLE_MATCH_RATIO]
        return hits, ("title" if hits else None)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def _same_local_day(a: datetime, b: datetime, tz: tzinfo) -> bool:
    return a.astimezone(tz).date() == b.astimezone(tz).date()


def _diff(extracted: dict, start: datetime, has_time: bool,
          live: dict, index: LiveIndex) -> dict:
    tz = index.tz
    changes: dict = {}
    live_start = live["_start"]
    day_moved = not _same_local_day(start, live_start, tz)
    time_moved = has_time and abs(start - live_start) > TIME_TOLERANCE
    if day_moved or time_moved:
        changes["start_at"] = {
            "live": live.get("start_at"),
            "live_local": live_start.astimezone(tz).isoformat(timespec="minutes"),
            "source": start.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "source_local": start.astimezone(tz).isoformat(timespec="minutes"),
        }
    src_venue = (extracted.get("venue") or "").strip()
    live_venue = index.venue_name_of(live)
    if src_venue and not names_match(src_venue, live_venue, VENUE_MATCH_RATIO):
        changes["venue"] = {
            "live": live_venue,
            "live_slug": live.get("venue_slug"),
            "source": src_venue,
        }
    return changes


def classify_all(extracted: list[dict], index: LiveIndex,
                 seed_url: Optional[str] = None,
                 now: Optional[datetime] = None) -> list[Classification]:
    """Classify every extraction from one seed against the live index."""
    tz = index.tz
    now = now or datetime.now(timezone.utc)
    out: list[Optional[Classification]] = [None] * len(extracted)
    pending: list[tuple[int, datetime, bool, list[dict], str]] = []
    claimed: set[int] = set()

    # Pass 1 — same-day matches claim their live occurrence.
    for i, ev in enumerate(extracted):
        start, has_time = parse_start(ev.get("date"), tz)
        if start is None:
            out[i] = Classification("skipped", ev, reason="unparseable date")
            continue
        if start.astimezone(tz).date() < now.astimezone(tz).date():
            out[i] = Classification("skipped", ev, reason="past event")
            continue
        cands, how = index.candidates(ev, seed_url)
        same_day = [c for c in cands
                    if id(c) not in claimed and _same_local_day(start, c["_start"], tz)]
        if same_day:
            live = min(same_day, key=lambda c: abs(c["_start"] - start))
            claimed.add(id(live))
            changes = _diff(ev, start, has_time, live, index)
            out[i] = Classification("changed" if changes else "unchanged", ev,
                                    live=live, changes=changes, matched_by=how)
        else:
            pending.append((i, start, has_time, cands, how or ""))

    # Pass 2 — a one-off live event, unclaimed and upcoming = rescheduled.
    today = now.astimezone(tz).date()
    for i, start, has_time, cands, how in pending:
        ev = extracted[i]
        live = cands[0] if len(cands) == 1 else None
        if live is not None and live.get("series_slug"):
            out[i] = Classification(
                "new", ev, reason=f"a new date in Series {live['series_slug']} "
                                  f"(live {live.get('slug')}) — a new occurrence, not a reschedule")
            continue
        if (live is not None and id(live) not in claimed
                and live["_start"].astimezone(tz).date() >= today
                and abs(live["_start"] - start) <= DATE_CHANGE_WINDOW):
            claimed.add(id(live))
            changes = _diff(ev, start, has_time, live, index)
            out[i] = Classification("changed", ev, live=live, changes=changes,
                                    matched_by=how,
                                    reason="single upcoming live occurrence nearby")
        else:
            reason = ("no live match" if not cands else
                      f"{len(cands)} live occurrence(s) of this key, none reschedulable — "
                      "treated as a new occurrence")
            out[i] = Classification("new", ev, reason=reason)
    return [c for c in out if c is not None]
