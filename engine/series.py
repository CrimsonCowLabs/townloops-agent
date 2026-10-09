"""TownLoops Series (ADR 0007), from the agent's side.

A **Series** is TownLoops' evergreen hub for the Events that recur under one
name. The agent never creates, renames or archives one — the API has no write
surface for Series and `townloops_api.Client` refuses one. An Event joins a
Series only through `create_event`'s auto-link: at create time its title's
`series_key` is compared with every *active* Series in the town. So what the
agent controls is the title it ships, and what it reads back is the Event's
`series_slug` / `series_id`.

This module holds the agent's Series judgement:

- `series_key` — TownLoops' normalization, reproduced, so a predicted
  auto-link matches the server's;
- the local Series snapshot (`state/series.json`, beside categories.json);
- `decide_title` — the title an occurrence ships under, and why;
- `shared_fill` — the blank shared fields an occurrence may take from its
  Series siblings;
- `candidates` — Clusters that look like a standing event but have no
  Series, and Series that have drifted (several venues, same-day copies),
  for a curator.

Vocabulary (TownLoops glossary): a **Series** gathers Events that already
exist; a **Cluster** is Events whose titles normalize to one key — a
candidate only; an **Event** is one dated occurrence.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html.entities import name2codepoint
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from engine import event_tickets
from engine.derive_helpers import normalize_name, parse_instant
from engine.policy import categories_snapshot_path


# --------------------------------------------------------------------------
# series_key — TownLoops `app/services/series.py:series_key`
# --------------------------------------------------------------------------
#
# TownLoops keys a title with python-slugify 8 (`slugify(title,
# lowercase=True, max_length=180)`, transliterating with text-unidecode).
# The steps below are that function's, in its order; tests/test_series.py
# pins the output on TownLoops' own cases and checks against the library
# whenever it is installed. text-unidecode is in the image; without it,
# NFKD plus a small table covers the Latin letters that don't decompose.

_KEY_MAX = 180
_QUOTES = re.compile(r"[\']+")
_ENTITY = re.compile(r"&(%s);" % "|".join(name2codepoint))
_DECIMAL = re.compile(r"&#(\d+);")
_HEX = re.compile(r"&#x([\da-fA-F]+);")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d)")
_DISALLOWED = re.compile(r"[^-a-zA-Z0-9]+")
_DASHES = re.compile(r"-{2,}")

try:  # pragma: no cover — depends on the environment
    from text_unidecode import unidecode as _unidecode
except ImportError:  # pragma: no cover
    try:
        from unidecode import unidecode as _unidecode
    except ImportError:
        _unidecode = None

# text-unidecode's answer for what NFKD leaves non-ASCII. Curly single
# quotes become `'`, which slugify then deletes ("Jon’s" → "jons").
_TRANSLIT = {"ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "œ": "oe", "Œ": "OE",
             "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "þ": "th",
             "Þ": "Th", "ı": "i", "‘": "'", "’": "'", "‚": "'", "‛": "'"}


def _ascii(text: str) -> str:
    if _unidecode is not None:
        return _unidecode(text)
    out = []
    for ch in text:
        if ch.isascii():
            out.append(ch)
        elif unicodedata.combining(ch):
            continue
        else:
            # Anything else unidecode would spell out or turn to punctuation;
            # as a separator it splits words the way punctuation does.
            out.append(_TRANSLIT.get(ch, "-"))
    return "".join(out)


def series_key(title: Optional[str]) -> str:
    """The normalized key TownLoops auto-links a title by."""
    text = _QUOTES.sub("-", title or "")
    text = _ascii(unicodedata.normalize("NFKD", text))
    text = _ENTITY.sub(lambda m: chr(name2codepoint[m.group(1)]), text)
    text = _DECIMAL.sub(lambda m: chr(int(m.group(1))), text)
    text = _HEX.sub(lambda m: chr(int(m.group(1), 16)), text)
    text = unicodedata.normalize("NFKD", text).lower()
    text = _QUOTES.sub("", text)
    text = _THOUSANDS.sub("", text)
    text = _DISALLOWED.sub("-", text)
    text = _DASHES.sub("-", text).strip("-")
    return text[:_KEY_MAX].strip("-")


# --------------------------------------------------------------------------
# Local Series snapshot
# --------------------------------------------------------------------------

SNAPSHOT_TTL_SECONDS = 600
_SNAPSHOT_FIELDS = ("id", "slug", "title", "cadence_text", "is_active")


def snapshot_path() -> Path:
    return categories_snapshot_path().parent / "series.json"


def refresh_snapshot(client, path: Optional[Path] = None,
                     now: Optional[float] = None) -> list[dict]:
    """Fetch the town's active Series (`GET /series` lists active only) and
    write the snapshot. API errors raise."""
    rows = [{k: s.get(k) for k in _SNAPSHOT_FIELDS} for s in client.paginate("series")
            if isinstance(s, dict) and s.get("slug")]
    path = path or snapshot_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time() if now is None else now,
                                   "series": rows}, indent=1))
        tmp.replace(path)
    except OSError:
        pass
    return rows


def load_snapshot(client=None, path: Optional[Path] = None,
                  ttl_seconds: int = SNAPSHOT_TTL_SECONDS,
                  now: Optional[float] = None) -> list[dict]:
    """The town's active Series. With a `client`, a snapshot older than
    `ttl_seconds` is refreshed first (the TTL spans a workqueue drain, which
    runs event-ship once per event); without one, whatever is on disk."""
    path = path or snapshot_path()
    now = time.time() if now is None else now
    try:
        data = json.loads(path.read_text())
        rows = data["series"]
        if client is None or now - float(data["fetched_at"]) < ttl_seconds:
            return rows
    except (OSError, ValueError, KeyError, TypeError):
        if client is None:
            return []
    return refresh_snapshot(client, path, now)


# --------------------------------------------------------------------------
# Title decision
# --------------------------------------------------------------------------

# A reworded title still counts as the Series' when it is this close.
CLOSE_TITLE_RATIO = 0.85


@dataclass
class TitleDecision:
    title: str
    reason: str                      # auto-link | series-title | performer-at-venue | source-title
    series_slug: Optional[str] = None   # the Series the shipped title will auto-link to

    def summary(self, source_title: str) -> str:
        to = f" → Series {self.series_slug}" if self.series_slug else " (no Series)"
        if self.title == source_title:
            return f"{self.reason}: {source_title!r}{to}"
        return f"{self.reason}: {source_title!r} shipped as {self.title!r}{to}"


def close_variant(key: str, other: str) -> bool:
    """`key` reads as a wording variant of the Series key `other`: the
    Series' words lead it ("trivia-thursday-week-3"), or the two are
    near-identical."""
    if not key or not other:
        return False
    a, b = key.split("-"), other.split("-")
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 2 and long_[:len(short)] == short:
        return True
    return SequenceMatcher(None, key, other).ratio() >= CLOSE_TITLE_RATIO


def source_branch(url: Optional[str]) -> Optional[str]:
    """`host/path` of a source URL minus a trailing dated/numbered segment
    (".../events/trivia/2026-10-09" → ".../events/trivia"), or None when what
    is left is too short to name one event's run (a bare "/events")."""
    if not url or not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return None
    segs = [s for s in parts.path.split("/") if s]
    if segs and re.search(r"\d", segs[-1]):
        segs = segs[:-1]
    if len(segs) < 2:
        return None
    return f"{parts.netloc.lower().removeprefix('www.')}/{'/'.join(segs).lower()}"


def _norm_url(url: Optional[str]) -> str:
    return (url or "").strip().rstrip("/")


def single_venue(occurrences: Iterable[dict]) -> Optional[int]:
    """The one venue a Series' occurrences are at, or None when none is
    resolved or they span several. Unresolved rows are a binding gap, not a
    second venue (TownLoops `cluster_verdict`)."""
    venues = {o.get("venue_id") for o in occurrences if o.get("venue_id") is not None}
    return venues.pop() if len(venues) == 1 else None


def _source_matches(source_url: Optional[str], seed_url: Optional[str],
                    occurrences: Iterable[dict]) -> bool:
    url = _norm_url(source_url)
    if not url or url == _norm_url(seed_url):
        return False   # a seed's listing page identifies nothing
    branch = source_branch(url)
    for o in occurrences:
        other = _norm_url(o.get("source_url"))
        if other and (other == url or (branch and source_branch(other) == branch)):
            return True
    return False


def venue_token(venue_id: Optional[int], venue_name: Optional[str]) -> Optional[str]:
    """How an event's venue is compared across local and live rows: its live
    id when known, else its normalized name."""
    if venue_id is not None:
        return f"id:{venue_id}"
    name = normalize_name(venue_name or "")
    return f"name:{name}" if name else None


def _with_venue_suffix(title: str, venue_name: str) -> str:
    suffix = f" at {venue_name.strip()}"
    if series_key(title).endswith(series_key(suffix)):
        return title
    return title.rstrip() + suffix


def active_series(series: Iterable[dict]) -> list[dict]:
    """The Series an occurrence may be linked to: archived ones only group."""
    return [s for s in series if s.get("is_active", True) is not False and s.get("slug")]


def decide_title(title: str, *, venue_id: Optional[int], venue_name: Optional[str],
                 source_url: Optional[str], performers: Iterable[str],
                 series: Iterable[dict], occurrences: Mapping[str, list[dict]],
                 cluster_venues: Iterable[str] = (), seed_url: Optional[str] = None,
                 ) -> TitleDecision:
    """The title to ship an occurrence under, so it auto-links into the right
    Series and not a wrong one. `series` is the snapshot, `occurrences` each
    candidate Series' live occurrences, `performers` the performer/creator
    names derived for this event, `cluster_venues` the `venue_token`s of
    other events (local or live) whose title has this one's key.

    - A performer-only title whose key matches an active Series held at
      another venue, or that recurs at other venues, ships as
      "<Performer> at <Venue>" — never twice.
    - A title with an active Series' key auto-links as it is.
    - A wording variant (or a title from the same source/source branch as
      the Series' occurrences) adopts the Series title only when the derived
      venue is the Series' one resolved venue.
    - Anything else keeps the source's title. Archived Series are never a
      target."""
    key = series_key(title)
    active = active_series(series)
    by_key = {series_key(s.get("title")): s for s in active}

    performer_only = bool(key) and any(series_key(p) == key for p in performers or ())
    if performer_only and venue_name and venue_name.strip():
        here = {venue_token(None, venue_name), venue_token(venue_id, None)}
        s = by_key.get(key)
        # Held at another venue: any resolved occurrence elsewhere — a
        # performer's Series already spanning venues included — or ours is
        # unknown, so the hub's venue can't be shown to be ours.
        held = {o.get("venue_id") for o in (occurrences.get(s["slug"], []) if s else [])} - {None}
        elsewhere = s is not None and (venue_id is None or bool(held - {venue_id}))
        if elsewhere or any(v and v not in here for v in cluster_venues):
            new = _with_venue_suffix(title, venue_name)
            target = by_key.get(series_key(new))
            return TitleDecision(new, "performer-at-venue", target and target["slug"])

    if key in by_key:
        return TitleDecision(title, "auto-link", by_key[key]["slug"])

    if venue_id is not None:
        for s in active:
            occ = occurrences.get(s["slug"])
            if not occ or single_venue(occ) != venue_id:
                continue
            if (close_variant(key, series_key(s.get("title")))
                    or _source_matches(source_url, seed_url, occ)):
                return TitleDecision(s["title"], "series-title", s["slug"])
    return TitleDecision(title, "source-title")


def candidate_slugs(title: str, source_url: Optional[str], series: Iterable[dict],
                    nearby: Iterable[dict], seed_url: Optional[str] = None) -> list[str]:
    """Active Series worth reading the occurrences of before `decide_title`:
    the one this title's key names, close variants of it, and Series whose
    nearby live occurrences share this event's source or source branch."""
    key = series_key(title)
    out: list[str] = []
    active = {s["slug"]: s for s in active_series(series)}
    for slug, s in active.items():
        skey = series_key(s.get("title"))
        if skey == key or close_variant(key, skey):
            out.append(slug)
    for r in nearby:
        slug = r.get("series_slug")
        if slug in active and slug not in out and _source_matches(source_url, seed_url, [r]):
            out.append(slug)
    return out


# --------------------------------------------------------------------------
# Series-shared fields
# --------------------------------------------------------------------------

SHARED_FIELDS = ("description", "category_slugs", "tags", "accessibility_notes")
# Tags that describe one date, not the Series.
OCCURRENCE_TAGS = frozenset({"free", "cancelled", "sold-out"})


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list)) and not value) or (
        isinstance(value, str) and not value.strip())


def is_cancelled(row: dict) -> bool:
    return (str(row.get("title") or "").startswith("Cancelled: ")
            or "cancelled" in (row.get("tags") or []))


def _shared_value(row: dict, field_: str) -> Any:
    v = row.get(field_)
    if field_ == "description" and v:
        v = event_tickets.without_ticket_line(v)
    elif field_ == "tags":
        v = [t for t in v or [] if t not in OCCURRENCE_TAGS]
    elif field_ == "category_slugs":
        v = list(v or [])
    return None if _blank(v) else v


def shared_fill(target: dict, siblings: Iterable[dict]) -> dict:
    """Overrides that fill `target`'s blank Series-shared fields from its
    siblings, the richest sibling first. Only blanks: a live value is never
    replaced. Never per-occurrence fields (date, times, slug, title, the
    ticket line, `free`/`cancelled`). The venue only when every sibling with
    a resolved venue is at the same one. Cancelled siblings aren't a source."""
    sibs = [s for s in siblings if s.get("slug") != target.get("slug") and not is_cancelled(s)]
    ranked = sorted(sibs, key=lambda s: (-sum(_shared_value(s, f) is not None
                                              for f in SHARED_FIELDS), str(s.get("slug"))))
    out: dict[str, Any] = {}
    for f in SHARED_FIELDS:
        if not _blank(target.get(f)):
            continue
        for s in ranked:
            v = _shared_value(s, f)
            if v is not None:
                out[f] = v
                break
    if target.get("venue_id") is None:
        # Every sibling counts here, cancelled ones too: a second venue in the
        # Series means none is asserted.
        venue = single_venue(s for s in siblings if s.get("slug") != target.get("slug"))
        if venue is not None:
            out["venue_id"] = venue
    return out


# --------------------------------------------------------------------------
# Candidates report
# --------------------------------------------------------------------------

@dataclass
class Cluster:
    key: str
    title: str
    dates: int
    venue_id: int
    venue_slug: Optional[str] = None
    slugs: list[str] = field(default_factory=list)


@dataclass
class SeriesDrift:
    slug: str
    venues: int = 0
    dates: list[str] = field(default_factory=list)   # town-local dates with 2+ occurrences


@dataclass
class CandidatesReport:
    clusters: list[Cluster] = field(default_factory=list)
    multi_venue: list[SeriesDrift] = field(default_factory=list)
    duplicate_dates: list[SeriesDrift] = field(default_factory=list)


def _local_date(row: dict, tz) -> Optional[str]:
    start = parse_instant(row.get("start_at"))
    return start.astimezone(tz).date().isoformat() if start else None


def candidates(events: Iterable[dict], tz, performers: Iterable[str] = ()) -> CandidatesReport:
    """Read-only judgement over live published Events, by TownLoops' own
    rule: a Cluster with no Series is a candidate when it has two or more
    distinct town-local dates at a single resolved Venue (rows whose venue
    never resolved don't count against it). Performer-name Clusters are left
    out. Existing Series that span several venues, or have two occurrences on
    one town-local date, are reported as drift."""
    performer_keys = {series_key(p) for p in performers}
    clusters: dict[str, list[dict]] = {}
    in_series: dict[str, list[dict]] = {}
    for e in events:
        if e.get("status") not in (None, "published"):
            continue
        if e.get("series_slug"):
            in_series.setdefault(e["series_slug"], []).append(e)
            continue
        key = series_key(e.get("title"))
        if key:
            clusters.setdefault(key, []).append(e)

    report = CandidatesReport()
    for key, rows in sorted(clusters.items()):
        if key in performer_keys:
            continue
        dates = {d for d in (_local_date(r, tz) for r in rows) if d}
        venues = {r.get("venue_id") for r in rows if r.get("venue_id") is not None}
        if len(dates) >= 2 and len(venues) == 1:
            title = min((r.get("title") or "" for r in rows), key=lambda t: (len(t), t))
            venue = venues.pop()
            venue_slug = next((r.get("venue_slug") for r in rows if r.get("venue_id") == venue), None)
            report.clusters.append(Cluster(key, title, len(dates), venue, venue_slug,
                                           sorted(r.get("slug") or "" for r in rows)))
    for slug, rows in sorted(in_series.items()):
        venues = {r.get("venue_id") for r in rows if r.get("venue_id") is not None}
        if len(venues) > 1:
            report.multi_venue.append(SeriesDrift(slug, venues=len(venues)))
        per_day: dict[str, int] = {}
        for r in rows:
            d = _local_date(r, tz)
            if d:
                per_day[d] = per_day.get(d, 0) + 1
        dup = sorted(d for d, n in per_day.items() if n > 1)
        if dup:
            report.duplicate_dates.append(SeriesDrift(slug, venues=len(venues), dates=dup))
    return report
