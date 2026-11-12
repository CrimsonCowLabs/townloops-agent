"""The one live-match module (#64): is this candidate already on TownLoops?

Every create path asks here before it submits anything that a ref or slug
lookup didn't find — organisation discovery and promote
(engine/business_discovery), the entity ship path (engine/entity_ship: hosts,
venues, performers, creators) and the event ship path's last fallback
(engine/live_lookup.find_live_event).

Entities (`LiveIndex`, ported from the CMO's `build_existing_index`):
  - same website, or a page under a live row's website path (a chain's
    per-location site `aleworx.com/stateline` covers only that location) —
    never on a host many entities share (SHARED_HOSTS, listing hosts);
  - else a name ≥ FUZZY_NAME_RATIO similar (normalized).
`match` is discovery's answer (the best row). `resolve` is the create
path's: a website match binds; name matches naming one place bind its
richest row; several different names, none exact, are `ambiguous` — no
create, no bind, recorded on the row (`mark_ambiguous`) for the analyst run
and the daily report. A name match whose own website is on another host
than the live row's is not the same entity.

Events: `titles_match` is the weekly reconcile's title rule
(engine/reconcile) and the ship path's; `event_matches` adds the same
town-local date, no clashing clock time, and the venue when both sides know
it.

One index per run: `live_rows` lists a resource once per client and caches
it beside the DB for LIVE_ROWS_TTL_SECONDS (a workqueue drain runs one ship
subprocess per event); `note_created` adds a row the run just created.
"""

from __future__ import annotations

import json
import os
import time
import weakref
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

from engine import discovery, listing_hosts
from engine.derive_helpers import (
    entity_richness, iso_utc_now, names_match, normalize_name, parse_extra_json,
    record_activity,
)


FUZZY_NAME_RATIO = 0.85
TITLE_MATCH_RATIO = 0.88
VENUE_MATCH_RATIO = 0.80
# Two stated start times further apart than this are two showings, not one.
TIME_TOLERANCE_MINUTES = 30
LIVE_ROWS_TTL_SECONDS = 600
WORKER = "live-dedup"
DEDUPED = "deduped-live"          # agent_activity action: a create skipped for a live match
AMBIGUOUS = "live-match-ambiguous"

# Hosts many businesses share: a website here identifies a business only by
# its exact page, never by the host. Listing hosts (engine.listing_hosts)
# are shared too — see _shared_host.
SHARED_HOSTS = {
    "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
    "youtube.com", "linktr.ee", "sites.google.com", "google.com",
}


@dataclass
class LiveMatch:
    slug: str
    name: str
    reason: str
    status: Optional[str] = None     # the live row's TownLoops status, when it has one


@dataclass
class Resolution:
    """A create path's answer: the live row to bind (`row`), or several
    plausible ones (`candidates`, ambiguous), or neither."""
    row: Optional[dict] = None
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""

    @property
    def ambiguous(self) -> bool:
        return self.row is None and len(self.candidates) > 1

    def slugs(self) -> list[str]:
        return sorted(str(c.get("slug")) for c in self.candidates)


def _shared_host(host: str) -> bool:
    return host in SHARED_HOSTS or listing_hosts.is_listing_host(host)


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def _canon(url: Optional[str]) -> Optional[str]:
    if not url or not isinstance(url, str) or not url.strip():
        return None
    try:
        return discovery.canonicalize_url(url)
    except ValueError:
        return None


class LiveIndex:
    """The town's live entities of one resource, indexed for dedup; `kind`
    names it in match reasons. `venues`, when given, is checked after the
    rows themselves (discovery: a candidate that is a live venue is never
    proposed either)."""

    def __init__(self, rows: list[dict], kind: str = "business",
                 venues: Optional["LiveIndex"] = None):
        self.rows = rows
        self.kind = kind
        self.venues = venues
        self._by_url: dict[str, dict] = {}
        self._by_site: dict[tuple[str, str], dict] = {}   # (host, path) → row
        for r in sorted(rows, key=entity_richness):   # richest row wins a shared key
            canon = _canon(r.get("website"))
            if not canon:
                continue
            self._by_url[canon] = r
            host = _host(canon)
            if host and not _shared_host(host):
                self._by_site[(host, urlparse(canon).path or "/")] = r

    @classmethod
    def build(cls, rows: Iterable[dict], kind: str = "business",
              venues: Optional[Iterable[dict]] = None) -> "LiveIndex":
        # A venue TownLoops rejected isn't a live venue.
        vindex = None if venues is None else cls(
            [v for v in venues if v.get("slug") and v.get("status") != "rejected"], "venue")
        return cls([r for r in rows if r.get("slug")], kind, vindex)

    # --- discovery ------------------------------------------------------------

    def match(self, canonical_url: str, name: Optional[str]) -> Optional[LiveMatch]:
        m = self._match(canonical_url, name)
        if m or self.venues is None:
            return m
        v = self.venues._match(canonical_url, name)
        if v is None:
            return None
        # Always `already-live`: a venue's own status says nothing about this candidate.
        return LiveMatch(v.slug, v.name, f"matches live venue {v.slug}: {v.reason}")

    def _match(self, canonical_url: str, name: Optional[str]) -> Optional[LiveMatch]:
        row, how = self._by_website(canonical_url)
        if row:
            return LiveMatch(row["slug"], row.get("name") or "", how, row.get("status"))
        best, best_rank = None, None
        for ratio, r in self._named(name):
            rank = (ratio, entity_richness(r))
            if best_rank is None or rank > best_rank:
                best, best_rank = r, rank
        if best is None:
            return None
        return LiveMatch(best["slug"], best.get("name") or "",
                         f"name fuzzy-matches live {self.kind} {best.get('name')!r} "
                         f"({best_rank[0]:.2f})", best.get("status"))

    # --- the create path --------------------------------------------------------

    def resolve(self, name: Optional[str], website: Optional[str] = None) -> Resolution:
        """The live row a local entity (`name`, `website`) already is, if any
        (module doc): website first, then the name; several different
        near-names with none exact are ambiguous."""
        canon = _canon(website) if not listing_hosts.is_listing_url(website) else None
        if canon:
            row, how = self._by_website(canon)
            if row:
                return Resolution(row, [row], how)
        own = _host(canon) if canon and not _shared_host(_host(canon)) else None
        hits = []
        for ratio, r in self._named(name):
            theirs = _canon(r.get("website"))
            if own and theirs and not _shared_host(_host(theirs)) and _host(theirs) != own:
                continue   # two different own sites: two different entities
            hits.append(r)
        if not hits:
            return Resolution()
        target = normalize_name(name or "")
        names = {normalize_name(r.get("name") or "") for r in hits}
        exact = [r for r in hits if normalize_name(r.get("name") or "") == target]
        if len(names) == 1 or exact:
            row = max(exact or hits, key=entity_richness)
            return Resolution(row, hits, f"name matches live {self.kind} {row.get('name')!r}")
        return Resolution(None, sorted(hits, key=lambda r: str(r.get("slug"))),
                          f"name close to several live {self.kind} rows")

    # --- shared rules -----------------------------------------------------------

    def _by_website(self, canonical_url: Optional[str]) -> tuple[Optional[dict], str]:
        if not canonical_url:
            return None, ""
        row = self._by_url.get(canonical_url)
        if row:
            return row, f"url matches live {self.kind} website"
        row = self._site_match(canonical_url)
        if row:
            return row, f"under live {self.kind} website {row.get('website')}"
        return None, ""

    def _named(self, name: Optional[str]) -> list[tuple[float, dict]]:
        target = normalize_name(name or "")
        if not target:
            return []
        out = []
        for r in self.rows:
            ratio = SequenceMatcher(None, target, normalize_name(r.get("name") or "")).ratio()
            if ratio >= FUZZY_NAME_RATIO:
                out.append((ratio, r))
        return out

    def _site_match(self, canonical_url: str) -> Optional[dict]:
        """The live row whose website contains this page: same host, and the
        page sits under the website's path. A chain's per-location website
        (`aleworx.com/stateline`) covers only that location's pages."""
        host = _host(canonical_url)
        if _shared_host(host):
            return None
        path = urlparse(canonical_url).path or "/"
        best, best_len = None, -1
        for (h, site_path), row in self._by_site.items():
            if h != host:
                continue
            if site_path == "/" or path == site_path or path.startswith(site_path + "/"):
                if len(site_path) > best_len:
                    best, best_len = row, len(site_path)
        return best


class VenueIndex:
    """A venue candidate's live dedup: the town's live venues, then its live
    businesses and communities (`orgs`) — the same match rules as
    LiveIndex."""

    def __init__(self, venues: LiveIndex, orgs: LiveIndex):
        self.rows, self.venues, self._v, self._orgs = venues.rows, None, venues, orgs

    def match(self, canonical_url: str, name: Optional[str]) -> Optional[LiveMatch]:
        m = self._v.match(canonical_url, name)
        if m:
            return m
        o = self._orgs.match(canonical_url, name)
        return o and LiveMatch(o.slug, o.name,
                               f"matches live organisation {o.slug}: {o.reason}")


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

def titles_match(a: Optional[str], b: Optional[str]) -> bool:
    """The event title rule (reconcile and ship): normalized titles at least
    TITLE_MATCH_RATIO similar."""
    na, nb = normalize_name(a or ""), normalize_name(b or "")
    return bool(na and nb and SequenceMatcher(None, na, nb).ratio() >= TITLE_MATCH_RATIO)


def event_matches(title: Optional[str], start_at: Optional[str], live: dict, *,
                  venue_id: Optional[int] = None, venue_slug: Optional[str] = None,
                  venue_name: Optional[str] = None,
                  live_venue_name: Optional[str] = None) -> bool:
    """Whether `live` is the event (`title`, `start_at`, its venue): titles
    match, the same town-local date, no clashing stated clock times (two
    showings), and — when both sides know it — the same venue (by id, else
    slug, else name)."""
    from engine.event_extract import day_and_clock
    if not titles_match(title, live.get("title")):
        return False
    ours, theirs = day_and_clock(start_at), day_and_clock(live.get("start_at"))
    if not ours or not theirs or ours[0] != theirs[0]:
        return False
    if ours[1] and theirs[1]:
        apart = abs((ours[1][0] * 60 + ours[1][1]) - (theirs[1][0] * 60 + theirs[1][1]))
        if apart > TIME_TOLERANCE_MINUTES:
            return False
    # The venue, by the strongest key both sides have: id, slug, name.
    if venue_id is not None and live.get("venue_id") is not None:
        return venue_id == live["venue_id"]
    if venue_slug and live.get("venue_slug"):
        return venue_slug == live["venue_slug"]
    lv = live_venue_name or live.get("venue_name")
    if venue_name and lv:
        return names_match(venue_name, lv, VENUE_MATCH_RATIO)
    return True


# --------------------------------------------------------------------------
# One index per run
# --------------------------------------------------------------------------

# client → {resource: rows}; weak, so a finished run's client takes its rows along.
_MEMO: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _memo(client) -> dict:
    try:
        return _MEMO.setdefault(client, {})
    except TypeError:   # not weak-referenceable: no memo
        return {}


def _cache_file(resource: str) -> Optional[Path]:
    """Beside the DB ($TOWNLOOPS_DB_PATH, else $HERMES_HOME/state), resolved
    per call; None when neither is set (memo only)."""
    db = os.environ.get("TOWNLOOPS_DB_PATH")
    if db:
        return Path(db).parent / "cache" / f"live-{resource}.json"
    home = os.environ.get("HERMES_HOME")
    return Path(home) / "state" / "cache" / f"live-{resource}.json" if home else None


def live_rows(client, resource: str, now: Optional[float] = None) -> list[dict]:
    """The town's live `resource` rows, listed at most once per run: memoized
    per client, and cached on disk for LIVE_ROWS_TTL_SECONDS. A failed list
    raises — a caller must not create on a lookup it couldn't make."""
    memo = _memo(client)
    if resource in memo:
        return memo[resource]
    now = time.time() if now is None else now
    path = _cache_file(resource)
    rows = None
    if path is not None:
        try:
            cached = json.loads(path.read_text())
            if (now - float(cached["fetched_at"]) < LIVE_ROWS_TTL_SECONDS
                    and cached.get("town") == os.environ.get("TOWNLOOPS_TOWN_SLUG")):
                rows = cached["rows"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if rows is None:
        rows = list(client.paginate(resource))
        _write_cache(path, rows, now)
    memo[resource] = rows
    return rows


def _write_cache(path: Optional[Path], rows: list[dict], now: float) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": now, "rows": rows,
                                   "town": os.environ.get("TOWNLOOPS_TOWN_SLUG")}))
        tmp.replace(path)
    except OSError:
        pass


def note_created(client, resource: str, row: dict) -> None:
    """A row this run created: later lookups in the run (and the cache) see it."""
    if not isinstance(row, dict) or not row.get("slug"):
        return
    memo = _memo(client)
    if resource in memo:
        memo[resource].append(row)
    path = _cache_file(resource)
    if path is None:
        return
    try:
        cached = json.loads(path.read_text())
        cached["rows"].append(row)
        _write_cache(path, cached["rows"], float(cached["fetched_at"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass


def reset() -> None:
    """Forget the in-process memo (a new run in the same process)."""
    _MEMO.clear()


def entity_index(client, resource: str, kind: str) -> LiveIndex:
    """The create path's index over `resource` — rows TownLoops rejected left
    out (a rejected row isn't a live one)."""
    return LiveIndex([r for r in live_rows(client, resource)
                      if r.get("slug") and r.get("status") != "rejected"], kind)


# --------------------------------------------------------------------------
# Records: deduped + ambiguous
# --------------------------------------------------------------------------

def record_dedup(conn, target: str, kind: str, slug: str, how: str) -> None:
    """A create skipped because `target` is already live as `slug`: one
    activity row naming the match (the daily report's "deduped")."""
    record_activity(conn, WORKER, DEDUPED, target,
                    detail=f"{kind} already live as {slug} ({how})")


def mark_ambiguous(conn, table: str, uid_col: str, uid: str, kind: str,
                   slugs: list[str], how: str) -> None:
    """Several plausible live rows: nothing created, nothing bound. Stamped
    on the row (`extra_json.live_match`) for the analyst run, with one
    failed activity row naming them."""
    row = conn.execute(f"SELECT extra_json FROM {table} WHERE {uid_col} = ?", (uid,)).fetchone()
    extra = parse_extra_json(row[0] if row else None)
    already = (extra.get("live_match") or {}).get("candidates") == slugs
    extra["live_match"] = {"status": "ambiguous", "candidates": slugs, "matched_by": how,
                           "at": iso_utc_now()}
    conn.execute(f"UPDATE {table} SET extra_json = ? WHERE {uid_col} = ?",
                 (json.dumps(extra), uid))
    if not already:
        target = uid if table == "events" else f"{kind}:{uid}"
        record_activity(conn, WORKER, AMBIGUOUS, target, ok=False,
                        error=f"{kind} may be live as any of {', '.join(slugs)} ({how}) — "
                              "not created, not bound")


def clear_ambiguous(conn, table: str, uid_col: str, uid: str) -> None:
    row = conn.execute(f"SELECT extra_json FROM {table} WHERE {uid_col} = ?", (uid,)).fetchone()
    extra = parse_extra_json(row[0] if row else None)
    if extra.pop("live_match", None) is not None:
        conn.execute(f"UPDATE {table} SET extra_json = ? WHERE {uid_col} = ?",
                     (json.dumps(extra) if extra else None, uid))


def ambiguous(conn) -> list[dict]:
    """Rows still waiting on an ambiguous live match (not shipped since):
    events and the five entity kinds."""
    out = []
    tables = (("event", "events", "event_uid", "COALESCE(title, name)", "state != 'shipped'"),
              ("venue", "venues", "entity_uid", "name", "townloops_slug IS NULL"),
              ("business", "businesses", "entity_uid", "name", "townloops_slug IS NULL"),
              ("community", "communities", "entity_uid", "name", "townloops_slug IS NULL"),
              ("performer", "performers", "entity_uid", "name", "townloops_slug IS NULL"),
              ("creator", "creators", "entity_uid", "name", "townloops_slug IS NULL"))
    for kind, table, uid_col, name, open_ in tables:
        for r in conn.execute(
                f"SELECT {uid_col}, {name}, json_extract(extra_json, '$.live_match') FROM {table} "
                f"WHERE json_valid(COALESCE(extra_json, '{{}}')) "
                f"AND json_extract(extra_json, '$.live_match.status') = 'ambiguous' AND {open_} "
                f"ORDER BY json_extract(extra_json, '$.live_match.at') DESC"):
            out.append({"kind": kind, "uid": r[0], "name": r[1], **json.loads(r[2])})
    return out
