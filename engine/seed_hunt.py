"""Seed hunting — find a town's event sources from evidence, daily, within a
Firecrawl budget (`event-seed-discovery hunt`, issue #64).

Seeds used to come only from a weekly Codex prompt answered from model
memory. `hunt` draws on four inputs (`INPUTS`), each a lazy stream of
calendar candidates, interleaved so no input starves the rest:

- `domains` — hosts the town's events link to (`events.source_url`) that
  aren't a seed yet (a syndicator's events pointing at a venue's own site):
  the event pages' shared listing path (`/events/…` → `/events`), else the
  calendar link on the host's homepage. Hosts with the most events first.
- `sites` — the websites of the venues, businesses and communities the town
  knows (local rows and live mirrors): the calendar link on each homepage,
  busiest entity first. A site already mined waits `RECHECK_DAYS`.
- `search` — web searches for the town's calendars (`queries`: events
  calendar, library, parks and recreation, the town's categories, its
  aliases, the profile's `seed_hunt.queries`), least recently run first, so
  successive days search something new.
- `memory` — the old Codex-from-memory proposer, at most weekly.

A candidate is never a listing/aggregator page (engine/listing_hosts), a
social platform, an article, or a page on a host that already has a seed —
in any status: a rejected or retired source is remembered and never
proposed again. One that survives is verified the way `discover` always did
(a rawHtml scrape, its feed probed — engine/seeds.record_feed) and inserted
`proposed`, its evidence recorded as a `seed:propose` decision; autonomous
`review` trials it the same day. `discovery_checks` remembers what was
looked at (queries, mined sites and domains, unreachable URLs) so the next
run moves on.

Network calls come in as `Tools`; every Firecrawl call — search, homepage
scrape, verify — spends one unit of the run's budget.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse, urlunparse

from engine import autonomy, categories, listing_hosts, own_site
from engine.derive_helpers import iso_utc_now, seed_ref
from engine.discovery import canonicalize_url, same_site
from engine.discovery_checks import checked_at, mark, recently_checked

INPUTS = ("domains", "sites", "search", "memory")
DEFAULT_INPUTS = INPUTS
# A mined site, mined domain or unreachable URL is looked at again after this.
RECHECK_DAYS = 30
MEMORY_EVERY_DAYS = 7
# A host events link to this often is worth a look.
MIN_DOMAIN_EVENTS = 2
SEARCH_RESULTS = 10

# A listing-level calendar path: the page that lists the events, not one
# event's page (`/events`, `/calendar`, `/whats-on`, `/events/month`).
_CALENDAR_SEG = re.compile(
    r"^(?:events?|calendars?|event-calendar|events-calendar|calendar-of-events|whats-?on"
    r"|shows|concerts|happenings|live-music|entertainment|programs|activities)"
    r"(?:\.(?:aspx?|php|html?|cfm))?$", re.IGNORECASE)
_LISTING_TAIL = re.compile(r"^(?:month|list|upcoming|all|all-events|calendar|index\.\w+)$",
                           re.IGNORECASE)
_EVENTISH = re.compile(r"\b(?:events?|calendars?|what'?s on|happenings|programs)\b",
                       re.IGNORECASE)
_KIND_HINTS = (
    ("library", re.compile(r"\blibrar(?:y|ies)\b", re.IGNORECASE)),
    ("chamber", re.compile(r"\bchamber\b", re.IGNORECASE)),
    ("parks", re.compile(r"\bparks?\b|\brecreation\b", re.IGNORECASE)),
    ("government", re.compile(r"\bcity of\b|\bcounty\b|\btown of\b|\bgovernment\b",
                              re.IGNORECASE)),
    ("newspaper", re.compile(r"\bnews\b|\btribune\b|\btimes\b|\bgazette\b|\bherald\b",
                             re.IGNORECASE)),
    ("tourism", re.compile(r"\bvisit\b|\btourism\b|\bvisitors?\b", re.IGNORECASE)),
)
# Hosts (with their subdomains) that serve many organisations' pages — a
# Google Calendar event, a link page: never one organisation's calendar site
# (a calendar feed on one is found by probing the organisation's own page).
_SHARED_HOSTS = ("google.com", "goo.gl", "linktr.ee", "bit.ly")
_TITLE_SPLIT = re.compile(r"\s+[|\-–—:]\s+")
_GENERIC_TITLE = re.compile(r"^(?:home|events?|calendars?|events? calendar|upcoming events"
                            r"|what'?s on|welcome)$", re.IGNORECASE)


@dataclass(frozen=True)
class Tools:
    search: Callable[[str], list[dict]]                    # query → [{url, title, description}]
    scrape: Callable[[str], dict]                          # url → {markdown, links, title, url}
    verify: Callable[[str], tuple[bool, int, str, str]]    # url → (ok, status, raw html, final)
    memory: Optional[Callable[[], list[dict]]] = None      # → [{name, url, kind}]


@dataclass
class Budget:
    """Firecrawl calls left in this run."""
    left: int
    spent: int = 0

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        self.spent += 1
        return True


@dataclass
class Lead:
    """A calendar page an input found, and why it thinks so."""
    url: str
    name: str
    kind: str
    via: str                     # discovered_by: domain | site | search | llm
    evidence: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Known seeds and page rules
# --------------------------------------------------------------------------

class KnownSeeds:
    """Every seed URL and host the town has, in any status — what a hunt
    never proposes again."""

    def __init__(self, conn: sqlite3.Connection):
        self.urls: set[str] = set()
        self.hosts: set[str] = set()
        for url, previous, feed in conn.execute(
                "SELECT url, previous_url, feed_url FROM event_seeds"):
            for u in (url, previous):
                if u:
                    self.add(u)
            if feed:
                self.urls.add(canonicalize_url(feed))

    def add(self, url: str) -> None:
        self.urls.add(canonicalize_url(url))
        host = listing_hosts.bare_host(url)
        if host:
            self.hosts.add(host)

    def has(self, url: str) -> bool:
        return canonicalize_url(url) in self.urls or listing_hosts.bare_host(url) in self.hosts


def not_a_source(url: str) -> Optional[str]:
    """Why `url` can't be one organisation's own events page, or None."""
    p = urlparse(url or "")
    if p.scheme not in ("http", "https") or not p.netloc:
        return "not an http(s) URL"
    if listing_hosts.is_listing_url(url) or listing_hosts.is_listing_host(
            listing_hosts.bare_host(url)):
        return "a listing/aggregator page"
    if own_site.platform(url):
        return "a social platform page"
    host = listing_hosts.bare_host(url)
    if any(host == h or host.endswith("." + h) for h in _SHARED_HOSTS):
        return "a host many organisations share"
    if own_site.is_article_page(url):
        return "an article"
    return None


def calendar_path(url: str) -> Optional[str]:
    """`url` cut back to its listing-level calendar path, or None: the
    first calendar segment and at most a listing tail after it
    (`/events/month`); one event's page (`/events/jazz-night`) gives its
    calendar (`/events`)."""
    p = urlparse(url)
    segs = [s for s in (p.path or "/").split("/") if s]
    for i, seg in enumerate(segs):
        if _CALENDAR_SEG.match(seg):
            keep = segs[:i + 1]
            if i + 1 < len(segs) and _LISTING_TAIL.match(segs[i + 1]):
                keep.append(segs[i + 1])
            if len(keep) == len(segs):   # already the calendar: its query may pick it
                return url
            return urlunparse((p.scheme, p.netloc, "/" + "/".join(keep), "", "", ""))
    return None


def is_calendar_page(url: str) -> bool:
    """`url` is itself a listing-level calendar page."""
    return calendar_path(url) == url


def calendar_link(site_url: str, links: list) -> Optional[str]:
    """The site's own calendar page among a homepage's `links`: same site,
    listing-level, shortest path first."""
    found = []
    for link in links or []:
        if not isinstance(link, str) or not same_site(link, listing_hosts.bare_host(site_url)):
            continue
        cut = calendar_path(link)
        if cut and not not_a_source(cut):
            found.append(cut)
    return min(found, key=lambda u: (len(urlparse(u).path), u)) if found else None


def guess_kind(text: str, url: str, default: str = "other") -> str:
    hay = f"{text} {listing_hosts.bare_host(url)}"
    if listing_hosts.bare_host(url).endswith(".gov") and not _KIND_HINTS[0][1].search(hay):
        return "government"
    return next((kind for kind, rx in _KIND_HINTS if rx.search(hay)), default)


def name_from_title(title: str, url: str) -> str:
    """A seed name from a page title: its first part that isn't a generic
    page label, else the host."""
    parts = [p.strip() for p in _TITLE_SPLIT.split(title or "") if p.strip()]
    named = [p for p in parts if not _GENERIC_TITLE.match(p)]
    return (named[0] if named else (title or "").strip() or listing_hosts.bare_host(url))[:120]


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def queries(profile: dict) -> list[str]:
    """The searches for the town's calendars."""
    town = profile.get("name") or profile.get("slug") or ""
    if not town:
        return []
    out = [f"{town} events calendar", f"{town} community calendar",
           f"{town} library events", f"{town} parks and recreation events",
           f"things to do in {town} this week", f"{town} live music calendar",
           f"{town} theater performances", f"{town} farmers market schedule"]
    out += [f"{town} {c['name']} events" for c in categories.town_categories() if c.get("name")]
    out += [f"{alias} events calendar" for alias in profile.get("name_aliases") or [] if alias]
    out += [q for q in ((profile.get("seed_hunt") or {}).get("queries") or []) if q]
    return list(dict.fromkeys(out))


def _search_leads(conn: sqlite3.Connection, profile: dict, tools: Tools,
                  budget: Budget) -> Iterator[Optional[Lead]]:
    ordered = sorted(queries(profile), key=lambda q: (checked_at(conn, f"query:{q}") or "", q))
    for q in ordered:
        if not budget.take():
            return
        results = tools.search(q) or []
        mark(conn, f"query:{q}", "searched", {"results": len(results)})
        yield None   # a search spent budget: let the other inputs have a turn
        for r in results[:SEARCH_RESULTS]:
            url, title = (r.get("url") or "").strip(), r.get("title") or ""
            if not url or not_a_source(url):
                continue
            if not (calendar_path(url) or _EVENTISH.search(title)):
                continue
            url = calendar_path(url) or url   # an event's page → its calendar
            yield Lead(url, name_from_title(title, url), guess_kind(title, url), "search",
                       {"query": q, "title": title[:200],
                        "description": (r.get("description") or "")[:300]})


# Entity table → the seed kind its calendar starts as (a library, a city
# department or a visitors' bureau is recognised by its name: guess_kind).
_ENTITY_SEED_KIND = {"venues": "venue", "businesses": "venue", "communities": "other"}


def _entities(conn: sqlite3.Connection) -> list[dict]:
    """The town's known organisations with a website, local rows and live
    mirrors alike — those events happen at most first."""
    rows = []
    for table, kind in _ENTITY_SEED_KIND.items():
        for uid, name, website, events in conn.execute(
                f"SELECT entity_uid, name, website, linked_event_count FROM {table} "
                "WHERE COALESCE(website, '') != ''"):
            rows.append({"entity_uid": uid, "name": name, "website": website,
                         "events": events or 0, "kind": kind, "table": table})
    return sorted(rows, key=lambda d: (-d["events"], d["table"], d["name"] or ""))


def _mine_site(conn: sqlite3.Connection, key: str, site: str, tools: Tools,
               budget: Budget) -> tuple[Optional[str], bool]:
    """The calendar on `site`'s homepage → (url or None, budget spent)."""
    if not budget.take():
        return None, False
    page = tools.scrape(site) or {}
    if not page:
        mark(conn, key, "unreachable", {"site": site})
        return None, True
    found = calendar_link(page.get("url") or site, page.get("links") or [])
    if not found:
        mark(conn, key, "no-calendar", {"site": site})
    return found, True


def _site_leads(conn: sqlite3.Connection, known: KnownSeeds, tools: Tools,
                budget: Budget) -> Iterator[Optional[Lead]]:
    seen: set[str] = set()
    for e in _entities(conn):
        site = e["website"].strip()
        host = listing_hosts.bare_host(site)
        if not host or host in seen or not_a_source(site) or known.has(site):
            continue
        seen.add(host)
        key = f"site:{host}"
        if recently_checked(conn, key, RECHECK_DAYS):
            continue
        evidence = {"entity": e["name"], "entity_uid": e["entity_uid"], "website": site,
                    "linked_events": e["events"]}
        kind = guess_kind(e["name"] or "", site, e["kind"])
        if is_calendar_page(site):
            yield Lead(site, e["name"] or host, kind, "site", evidence)
            continue
        found, spent = _mine_site(conn, key, site, tools, budget)
        if not spent:
            return
        if found:
            mark(conn, key, "calendar-found", {"site": site, "calendar": found})
            yield Lead(found, e["name"] or host, kind, "site", evidence)
        else:
            yield None


def _domain_leads(conn: sqlite3.Connection, known: KnownSeeds, tools: Tools,
                  budget: Budget) -> Iterator[Optional[Lead]]:
    by_host: dict[str, list[str]] = {}
    for (url,) in conn.execute("SELECT source_url FROM events WHERE COALESCE(source_url, '') != ''"):
        host = listing_hosts.bare_host(url)
        if host and not known.has(url) and not not_a_source(url):
            by_host.setdefault(host, []).append(url)
    for host, urls in sorted(by_host.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(urls) < MIN_DOMAIN_EVENTS:
            break
        key = f"domain:{host}"
        if recently_checked(conn, key, RECHECK_DAYS):
            continue
        evidence = {"host": host, "events": len(urls), "examples": urls[:3]}
        paths = Counter(c for c in (calendar_path(u) for u in urls) if c)
        if paths:
            url = paths.most_common(1)[0][0]
        else:
            scheme = urlparse(urls[0]).scheme or "https"
            url, spent = _mine_site(conn, key, f"{scheme}://{urlparse(urls[0]).netloc}/",
                                    tools, budget)
            if not spent:
                return
            if not url:
                yield None
                continue
        mark(conn, key, "calendar-found", {**evidence, "calendar": url})
        yield Lead(url, host, guess_kind("", url, "venue"), "domain", evidence)


def _memory_leads(conn: sqlite3.Connection, tools: Tools) -> Iterator[Optional[Lead]]:
    if not tools.memory or recently_checked(conn, "memory", MEMORY_EVERY_DAYS):
        return
    proposals = tools.memory() or []
    mark(conn, "memory", "asked", {"proposals": len(proposals)})
    for p in proposals:
        if isinstance(p, dict) and p.get("url") and p.get("name"):
            yield Lead(p["url"].strip(), str(p["name"]).strip()[:200],
                       (p.get("kind") or "other").strip().lower(), "llm", {"source": "model memory"})


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

VALID_KINDS = {"chamber", "government", "tourism", "library", "parks", "newspaper", "venue",
               "other"}


def _unique_name(conn: sqlite3.Connection, name: str, url: str) -> str:
    """Seeds are referred to by name (`seed:<name>`): a name already taken
    gets the host."""
    if not conn.execute("SELECT 1 FROM event_seeds WHERE name = ?", (name,)).fetchone():
        return name
    return f"{name} ({listing_hosts.bare_host(url)})"[:200]


def propose(conn: sqlite3.Connection, lead: Lead, known: KnownSeeds, tools: Tools,
            budget: Budget, record_feed: Callable) -> str:
    """Verify one lead and insert it `proposed` → its outcome: `proposed`,
    `known-seed`, `not-a-source`, `unreachable`, `recently-unreachable`, or
    `out-of-budget`."""
    if not_a_source(lead.url):
        return "not-a-source"
    if known.has(lead.url):
        return "known-seed"
    key = f"url:{canonicalize_url(lead.url)}"
    if recently_checked(conn, key, RECHECK_DAYS):
        return "recently-unreachable"
    if not budget.take():
        return "out-of-budget"
    ok, code, html, final = tools.verify(lead.url)
    if not ok:
        mark(conn, key, "unreachable", {"status": code, "via": lead.via})
        return "unreachable"
    if final and canonicalize_url(final) != canonicalize_url(lead.url) and known.has(final):
        known.add(lead.url)
        return "known-seed"
    now = iso_utc_now()
    name = _unique_name(conn, lead.name, lead.url)
    kind = lead.kind if lead.kind in VALID_KINDS else "other"
    evidence = {"via": lead.via, "url": lead.url, **lead.evidence}
    conn.execute("INSERT INTO event_seeds (name, url, kind, status, discovered_by, notes, "
                 "first_seen_at, last_seen_at, status_note, status_changed_at) "
                 "VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?)",
                 (name, lead.url, kind, lead.via, json.dumps(evidence, ensure_ascii=False)[:2000],
                  now, now, f"[hunt] found via {lead.via}", now))
    record_feed(conn, lead.url, html, final or lead.url)
    autonomy.record(conn, "seed", seed_ref(name),
                    autonomy.Decision("propose", f"found-via-{lead.via}", evidence))
    conn.commit()
    known.add(lead.url)
    return "proposed"


def hunt(conn: sqlite3.Connection, profile: dict, tools: Tools, *, max_firecrawl: int,
         max_proposals: int, inputs: tuple[str, ...] = DEFAULT_INPUTS,
         record_feed: Callable) -> dict:
    """One hunt: interleave the `inputs`' leads, propose each new one, stop
    at the budget or `max_proposals`. Returns counts and the proposals."""
    budget = Budget(max_firecrawl)
    known = KnownSeeds(conn)
    streams = {
        "domains": lambda: _domain_leads(conn, known, tools, budget),
        "sites": lambda: _site_leads(conn, known, tools, budget),
        "search": lambda: _search_leads(conn, profile, tools, budget),
        "memory": lambda: _memory_leads(conn, tools),
    }
    live = [(name, streams[name]()) for name in INPUTS if name in inputs]
    out: dict = {"proposed": 0, "outcomes": Counter(), "by_input": Counter(), "seeds": []}
    while live and out["proposed"] < max_proposals:
        for name, stream in list(live):
            lead = next(stream, StopIteration)
            if lead is StopIteration:
                live.remove((name, stream))
                continue
            if lead is None:
                continue
            outcome = propose(conn, lead, known, tools, budget, record_feed)
            out["outcomes"][outcome] += 1
            if outcome == "proposed":
                out["proposed"] += 1
                out["by_input"][name] += 1
                out["seeds"].append({"name": lead.name, "url": lead.url, "via": lead.via})
                if out["proposed"] >= max_proposals:
                    break
            if budget.left <= 0 and outcome == "out-of-budget":
                live = []
                break
    return {"proposed": out["proposed"], "firecrawl_calls": budget.spent,
            "by_input": dict(out["by_input"]), "outcomes": dict(out["outcomes"]),
            "seeds": out["seeds"]}
