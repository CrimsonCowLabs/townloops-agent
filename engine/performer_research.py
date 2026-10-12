"""Performer / creator research along a strict evidence ladder.

Ported from the CMO's `townloops-research/references/performer-source-drill.md`.
A performer's name alone is weak evidence — common names and stage names
match strangers — so identity is attached only when the evidence bridges a
public profile to the local booking:

1. **Event evidence** (local DB, no network): the linked events, their
   venues, how often they recur, the event pages.
2. **Venue / event pages**: a link the venue itself gives to the
   performer's own profile/site is a bridge. A booking with no link is event
   evidence only, never identity proof.
3. **Public-profile metadata**: the profile page's title and meta
   description — exact name, plus locality (the town or a linked venue)
   and role (music, art, …).
4. **Search, last**: used only to *discover* candidate profiles; a search
   snippet never verifies anything — the candidate's own page must.

Outcomes: `verified` fills blank fields; `probable` (name + role, no
locality bridge) and `ambiguous` (several fits, or only a name) go to the
operator (`enrichment_status = 'needs-review'`); `unresolved` is
`not-found`; `deferred` (budget out, pages unreadable) stays `pending` for a
later run. Description/bio is only ever verbatim text from the identified
profile — null beats invented copy. Never writes to TownLoops; filled
fields reach live through engine/entity_sync.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Callable, Literal, Optional
from urllib.parse import urljoin, urlparse

from engine import listing_hosts, research
from engine.listing_hosts import bare_host as _host
from engine.derive_helpers import iso_utc_now, normalize_name, parse_extra_json
from engine.entity_enrich import SPECS, Budget, fill_blanks, validate_payload

KINDS = ("performer", "creator")
# The review queue also holds the CMO's imported community drafts
# (engine/cmo_import).
REVIEW_KINDS = (*KINDS, "community")
REVIEW_STATUS = "needs-review"
REVIEW_FINDINGS = ("probable", "ambiguous")
DEFAULT_LOOKUPS = 6
# A row deferred this many runs in a row (its candidates never readable)
# is given up as unresolved, so it stops spending lookups every run.
MAX_DEFERRALS = 3
_MAX_VENUE_PAGES = 2
_MAX_SEARCH_CANDIDATES = 3

Fetch = Callable[[str], tuple[Optional[str], Optional[str]]]
Search = Callable[[str], list[dict]]

Status = Literal["verified", "probable", "ambiguous", "unresolved", "deferred"]

_ROLE_TERMS = {
    # Words that say "this is an act", not bare genres ("country living",
    # "rock climbing") or "tour" as in tourism.
    "performer": r"music|musician\w*|band|singer\w*|songwriter\w*|dj|guitarist|bassist|drummer|"
                 r"vocalist|acoustic|cover band|rapper|comedian|stand-up|performer|"
                 r"tour|touring|tour dates|live shows?",
    "creator": r"artist\w*|ceramicist|potter|photographer|author|writer|illustrator|painter|"
               r"sculptor|maker|creator|podcast\w*|filmmaker|designer|instructor",
}

# host → field. Anything else is the entity's own website.
_LINK_FIELDS = {
    "instagram.com": "instagram", "facebook.com": "facebook", "tiktok.com": "tiktok",
    "twitter.com": "twitter", "x.com": "twitter", "linkedin.com": "linkedin",
    "spotify.com": "spotify_url", "youtube.com": "youtube", "youtu.be": "youtube",
}
_EVENT_PATH = re.compile(
    r"/(?:events?|e|tickets?|shows?|concerts?|calendar|tour-dates|whats-on)(?:/|$)", re.IGNORECASE)
# What a profile (not a post, event or video) looks like on each platform.
_PROFILE_PATH = {
    "instagram": r"/[\w.]+/?", "tiktok": r"/@[\w.]+/?", "twitter": r"/\w+/?",
    "facebook": r"/(?!events\b|groups\b|share\b|watch\b|photo|story|profile\.php)[\w.\-]+/?",
    "spotify_url": r"/artist/\w+/?",
    "youtube": r"/(?:@[\w.\-]+|channel/[\w\-]+|c/[\w.\-]+|user/[\w.\-]+)/?",
    "linkedin": r"/in/[\w\-]+/?",
}
# Their image URLs are signed CDN links that expire — never store them.
_EXPIRING_IMAGE_HOSTS = ("instagram.com", "facebook.com")

_IG_BIO = re.compile(r'on Instagram:\s*"(?P<bio>.+)"\s*$', re.DOTALL)
_IG_BOILERPLATE = re.compile(r"^[\d.,]+[KkMm]?\s+Followers.*See Instagram photos and videos", re.DOTALL)
_FB_PREFIX = re.compile(r"^.*?[\d.,]+[KkMm]?\s+likes?\s*·[^.]*\.\s*", re.DOTALL)
_ANCHOR = re.compile(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")


@dataclass
class Evidence:
    event_count: int = 0
    venues: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    url: str
    source: Literal["venue-page", "search"]
    read: bool = False
    name_match: bool = False
    locality: bool = False
    role: bool = False
    bridge: bool = False          # the venue/event page linked to it
    fields: dict = field(default_factory=dict)   # what it would fill, if accepted

    @property
    def evidential(self) -> bool:
        """Read, names the performer, and says something about them — an
        empty shell matching only the name is not evidence."""
        return self.read and self.name_match and (self.role or self.locality)

    @property
    def verified(self) -> bool:
        return self.name_match and (self.bridge or (self.locality and self.role))


@dataclass
class Finding:
    status: Status
    payload: dict = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    why: str = ""
    description_source: Optional[str] = None
    evidence: Evidence = field(default_factory=Evidence)


def event_evidence(conn: sqlite3.Connection, kind: str, entity_uid: str) -> Evidence:
    """Rung 1: the entity's linked events, their venues and event pages."""
    rows = conn.execute(
        "SELECT e.event_uid, e.name, e.source_url, "
        "  (SELECT v.name FROM linked_events lv JOIN venues v ON v.entity_uid = lv.entity_uid "
        "   WHERE lv.event_uid = e.event_uid AND lv.entity_kind = 'venue' LIMIT 1) AS venue "
        "FROM linked_events l JOIN events e ON e.event_uid = l.event_uid "
        "WHERE l.entity_kind = ? AND l.entity_uid = ? ORDER BY e.date, e.event_uid",
        (kind, entity_uid),
    ).fetchall()
    ev = Evidence(event_count=len({r["event_uid"] for r in rows}))
    for r in rows:
        for value, bucket in ((r["venue"], ev.venues), (r["source_url"], ev.source_urls),
                              (r["name"], ev.titles)):
            if value and value not in bucket:
                bucket.append(value)
    ev.venues.sort()
    return ev


def _platform(url: str) -> Optional[str]:
    host = _host(url)
    for h, fld in _LINK_FIELDS.items():
        if host == h or host.endswith("." + h):
            return fld
    return None


def _has_phrase(phrase: str, text: str) -> bool:
    p = normalize_name(phrase)
    return bool(p) and f" {p} " in f" {normalize_name(text)} "


def _compact(s: str) -> str:
    return normalize_name(s).replace(" ", "")


def _handle_matches(name: str, url: str) -> bool:
    compact = _compact(name)
    return len(compact) >= 5 and compact in _compact(f"{_host(url)} {urlparse(url).path}")


def _own_host(name: str, url: str) -> bool:
    """The site's host carries the name: moe.org for "moe.",
    janedoe.bandcamp.com / janedoemusic.com for "Jane Doe"."""
    host, compact = _host(url), _compact(name)
    return bool(compact) and (host.split(".")[0].replace("-", "") == compact
                              or (len(compact) >= 5 and compact in host.replace("-", "").replace(".", "")))


def is_presence(name: str, url: str, *, bridged: bool, exclude_hosts: set[str] = frozenset()) -> bool:
    """Could `url` be the performer's own presence? A platform profile (not
    a post/event/video), or — never on a ticketing/listing host or an
    event path — a site the venue linked to (`bridged`) or whose host
    carries the name."""
    host, path = _host(url), urlparse(url).path or "/"
    # Ticketing / listing hosts name the performer and the town, but they
    # are a booking, not the performer.
    if host in exclude_hosts or listing_hosts.is_listing_host(host):
        return False
    platform = _platform(url)
    if platform:
        return bool(re.fullmatch(_PROFILE_PATH[platform], path))
    if _EVENT_PATH.search(path):
        return False
    if _own_host(name, url):
        return True
    # A venue's link to someone else's site counts only as a homepage — a
    # deeper page (an article, a review) is about them, not theirs.
    return bridged and path.strip("/") == ""


def venue_page_links(html: str, page_url: str, name: str) -> list[str]:
    """Rung 2: links on a venue/event page to the performer's own
    profile/site — off the venue's host, labelled with the performer's
    name or carrying it in the URL, and not a ticket/listing page."""
    out: list[str] = []
    page_host = _host(page_url)
    for href, text in _ANCHOR.findall(html):
        url = urljoin(page_url, html_lib.unescape(href))
        if not url.startswith(("http://", "https://")) or _host(url) == page_host:
            continue
        label = html_lib.unescape(_TAGS.sub(" ", text))
        if ((_has_phrase(name, label) or _handle_matches(name, url)) and url not in out
                and is_presence(name, url, bridged=True)):
            out.append(url)
    return out


def _profile_description(meta_description: str) -> str:
    """The profile's own words from its meta description, verbatim — the
    Instagram bio inside its follower-count wrapper, a Facebook page's text
    after its like-count prefix. Boilerplate with no bio → ""."""
    text = html_lib.unescape(meta_description or "").strip()
    if _IG_BOILERPLATE.match(text):
        return ""
    m = _IG_BIO.search(text)
    if m:
        return m.group("bio").strip()
    if re.match(r"^[\d.,]+[KkMm]?\s+Followers", text):
        return ""
    return _FB_PREFIX.sub("", text, count=1).strip()


def _read_profile(cand: Candidate, html: str, final_url: str, kind: str, name: str,
                  evidence: Evidence, town: str) -> None:
    """Rung 3: judge a candidate from its page title and meta description."""
    og, meta = research.og_tags(html), research.meta_tags(html)
    title = " ".join(filter(None, (research.title_text(html), og.get("title"))))
    title = html_lib.unescape(title)
    description = _profile_description(og.get("description") or meta.get("description") or "")
    blob = f"{title} {description}"
    cand.read = True
    cand.name_match = _has_phrase(name, title) or (cand.bridge and _handle_matches(name, cand.url))
    cand.locality = any(_has_phrase(p, blob) for p in (town, *evidence.venues) if p)
    cand.role = bool(re.search(rf"(?<!\w)(?:{_ROLE_TERMS[kind]})(?!\w)", blob, re.IGNORECASE))
    if description:
        cand.fields["description"] = description
    if not _platform(cand.url):
        for block in research.ld_json_blocks(html):
            research._fill_from_ldjson(cand.fields, block)   # sameAs socials
    if not any(h in _host(final_url) for h in _EXPIRING_IMAGE_HOSTS):
        for image in research.image_candidates(html, final_url):
            if research.verify_image(image):
                cand.fields["image_path"] = image
                cand.fields["image_alt"] = name
                break


def _fields_for(kind: str, cand: Candidate) -> dict:
    """The candidate's link plus what its page gave, as `kind` columns,
    schema-validated."""
    raw = dict(cand.fields)
    platform = _platform(cand.url)
    if platform:
        raw[platform] = cand.url
    else:
        p = urlparse(cand.url)
        raw["website"] = f"{p.scheme}://{p.netloc}/"
    if kind == "creator":
        for src, dst in (("description", "bio"), ("youtube", "youtube_url")):
            if src in raw:
                raw.setdefault(dst, raw.pop(src))
    clean, _ = validate_payload(kind, raw)
    return {k: v for k, v in clean.items() if k in SPECS[kind].fillable}


def _default_fetch(url: str) -> tuple[Optional[str], Optional[str]]:
    html, final = research.fetch_html(url)
    if html:
        return html, final
    return research.firecrawl_fetch(url)


def _default_search(query: str) -> list[dict]:
    from engine import discovery
    return discovery.firecrawl_search(query, limit=5, raise_on_error=True)


def investigate(conn: sqlite3.Connection, kind: str, entity_uid: str, town: str,
             budget: Budget, *, fetch: Optional[Fetch] = None,
             search: Optional[Search] = None) -> Finding:
    """Walk the ladder for one performer/creator. Pure research: writes
    nothing (see apply_finding)."""
    from engine import discovery
    fetch = fetch or _default_fetch
    search = search or _default_search
    row = conn.execute(f"SELECT name FROM {SPECS[kind].table} WHERE entity_uid = ?",
                       (entity_uid,)).fetchone()
    name = row["name"]
    evidence = event_evidence(conn, kind, entity_uid)
    cands: dict[str, Candidate] = {}
    out_of_budget = False

    # Rung 2 — what the venue/event pages link to.
    for page_url in evidence.source_urls[:_MAX_VENUE_PAGES]:
        if not budget.take():
            out_of_budget = True
            break
        html, final = fetch(page_url)
        if html:
            for url in venue_page_links(html, final or page_url, name):
                cands.setdefault(url, Candidate(url, "venue-page", bridge=True))

    def read(cand: Candidate) -> bool:
        if not budget.take():
            return False
        html, final = fetch(cand.url)
        if html:
            _read_profile(cand, html, final or cand.url, kind, name, evidence, town)
        elif cand.bridge:
            # Unreadable (profiles often wall logged-out readers), but the
            # venue linked it and the handle carries the name.
            cand.name_match = _handle_matches(name, cand.url)
        return True

    for cand in list(cands.values()):
        if not read(cand):
            out_of_budget = True

    # Rung 4 — search, only to find candidates when no bridge verified one.
    search_error = None
    if not any(c.verified for c in cands.values()) and not out_of_budget:
        if budget.take():
            try:
                hits = search(f'"{name}" {town}'.strip())
            except (discovery.FirecrawlError, TimeoutError) as e:
                if re.search(r"\[http 40[13]\]", str(e)):
                    # Bad key: deferring would retry every row forever.
                    raise research.FirecrawlAuthError(str(e)) from e
                hits, search_error = [], str(e)
            if not hits and not search_error:
                # Throttled search backends answer with nothing; that's not
                # proof of no public presence (MAX_DEFERRALS bounds retries).
                search_error = "search returned no results"
            booked_on = {_host(u) for u in evidence.source_urls}
            found = [h["url"] for h in hits if h.get("url")
                     and not discovery.host_blocked(h["url"], allow_gov=True)
                     and is_presence(name, h["url"], bridged=False, exclude_hosts=booked_on)
                     and _has_phrase(name, h.get("title") or "")]
            for url in found[:_MAX_SEARCH_CANDIDATES]:
                if url in cands:
                    continue
                cand = cands[url] = Candidate(url, "search")
                if not read(cand):
                    out_of_budget = True
                    break
        else:
            out_of_budget = True

    candidates = list(cands.values())
    for c in candidates:
        c.fields = _fields_for(kind, c)
    finding = _classify(kind, candidates, evidence)
    if finding.status == "verified":
        return finding
    unread = [c for c in candidates if not c.read and not c.name_match]
    if out_of_budget or search_error or (unread and not any(c.evidential for c in candidates)):
        why = ("budget ran out" if out_of_budget else
               f"search failed: {search_error}" if search_error else
               "candidate profile pages could not be read")
        return Finding("deferred", candidates=candidates, why=why, evidence=evidence)
    return finding


def _classify(kind: str, candidates: list[Candidate], evidence: Evidence) -> Finding:
    verified = [c for c in candidates if c.verified]
    fits = [c for c in candidates if c.evidential]
    if verified:
        payload: dict = {}
        source = None
        for c in verified:
            for k, v in c.fields.items():
                if k in ("description", "bio", "image_path", "image_alt"):
                    if k not in payload:
                        payload[k] = v
                        if k in ("description", "bio"):
                            source = c.url
                elif payload.setdefault(k, v) != v:
                    return Finding("ambiguous", candidates=candidates, evidence=evidence,
                                   why=f"two verified profiles disagree on {k}: "
                                       f"{payload[k]} vs {v}")
        how = ("linked from the venue page" if any(c.bridge for c in verified)
               else "profile names the town/venue and the role")
        return Finding("verified", payload, candidates, why=how,
                       description_source=source, evidence=evidence)
    if len(fits) == 1:
        missing = "the town or a linked venue" if fits[0].role else "a role"
        return Finding("probable", candidates=candidates, evidence=evidence,
                       why=f"{fits[0].url} matches the name but doesn't name {missing}")
    if fits:
        return Finding("ambiguous", candidates=candidates, evidence=evidence,
                       why=f"{len(fits)} public profiles match the name; none is "
                           "bridged to the booking")
    return Finding("unresolved", candidates=candidates, evidence=evidence,
                   why="no public profile names this " + kind)


def _extra(conn: sqlite3.Connection, table: str, entity_uid: str) -> dict:
    row = conn.execute(f"SELECT extra_json FROM {table} WHERE entity_uid = ?",
                       (entity_uid,)).fetchone()
    return parse_extra_json(row and row["extra_json"])


def _record(conn: sqlite3.Connection, kind: str, entity_uid: str, record: dict) -> None:
    table = SPECS[kind].table
    extra = _extra(conn, table, entity_uid)
    extra["research"] = {**record, "at": iso_utc_now()}
    conn.execute(f"UPDATE {table} SET extra_json = ? WHERE entity_uid = ?",
                 (json.dumps(extra, ensure_ascii=False, sort_keys=True), entity_uid))


_STATUS_FOR = {"verified": "enriched", "unresolved": "not-found",
               **{f: REVIEW_STATUS for f in REVIEW_FINDINGS}}


def apply_finding(conn: sqlite3.Connection, kind: str, entity_uid: str,
                  finding: Finding) -> list[str]:
    """Write a finding: verified fills blank fields; review/unresolved set
    the status only; deferred leaves the row pending. Every outcome is
    recorded in `extra_json.research` for the operator; the
    MAX_DEFERRALS-th deferral in a row is recorded as unresolved. Returns
    the columns filled."""
    filled: list[str] = []
    deferrals = 0
    if finding.status == "deferred":
        prior = _extra(conn, SPECS[kind].table, entity_uid).get("research") or {}
        deferrals = int(prior.get("deferrals") or 0) + 1
        if deferrals >= MAX_DEFERRALS:
            finding = Finding("unresolved", candidates=finding.candidates,
                              evidence=finding.evidence,
                              why=f"gave up after {deferrals} deferred runs: {finding.why}")
    if finding.status in _STATUS_FOR:
        payload = finding.payload if finding.status == "verified" else {}
        filled = fill_blanks(conn, kind, entity_uid, payload, _STATUS_FOR[finding.status])
    ev = finding.evidence
    _record(conn, kind, entity_uid, {
        "status": finding.status, "why": finding.why,
        "event_count": ev.event_count, "venues": ev.venues,
        "event_urls": ev.source_urls[:5],
        "candidates": [{k: v for k, v in asdict(c).items() if k != "read"}
                       for c in finding.candidates],
        "description_source": finding.description_source,
        "filled": filled,
        **({"deferrals": deferrals} if finding.status == "deferred" else {}),
    })
    return filled


def next_batch(conn: sqlite3.Connection, limit: int) -> list[tuple[str, str]]:
    """Pending performers/creators: never-attempted ones first, most booked
    first; then attempted ones, longest since the last attempt first (so
    deferred rows take turns). Live mirrors excluded."""
    parts = [
        f"SELECT '{kind}' AS kind, t.entity_uid, t.name, "
        f"  json_extract(COALESCE(t.extra_json, '{{}}'), '$.research.at') AS tried, "
        f"  (SELECT COUNT(DISTINCT event_uid) FROM linked_events l "
        f"   WHERE l.entity_kind = '{kind}' AND l.entity_uid = t.entity_uid) AS n "
        f"FROM {SPECS[kind].table} t "
        f"WHERE t.enrichment_status = 'pending' AND t.source_kind != 'live'"
        for kind in KINDS
    ]
    rows = conn.execute(
        "SELECT * FROM (" + " UNION ALL ".join(parts) + ") "
        "ORDER BY tried IS NOT NULL, CASE WHEN tried IS NULL THEN n END DESC, tried, name "
        "LIMIT ?", (limit,),
    ).fetchall()
    return [(r["kind"], r["entity_uid"]) for r in rows]


def run_batch(conn: sqlite3.Connection, town: str, *, limit: int = 3,
              lookups: int = DEFAULT_LOOKUPS, fetch: Optional[Fetch] = None,
              search: Optional[Search] = None) -> list[dict]:
    """Research and apply the next `limit` performers/creators, each with
    its own lookup budget."""
    results = []
    for kind, uid in next_batch(conn, limit):
        finding = investigate(conn, kind, uid, town, Budget(lookups), fetch=fetch, search=search)
        filled = apply_finding(conn, kind, uid, finding)
        name = conn.execute(f"SELECT name FROM {SPECS[kind].table} WHERE entity_uid = ?",
                            (uid,)).fetchone()["name"]
        results.append({"kind": kind, "entity_uid": uid, "name": name,
                        "status": finding.status, "filled": filled, "why": finding.why})
    return results


def review_queue(conn: sqlite3.Connection) -> list[dict]:
    """Entities waiting for an operator identity decision: performers and
    creators this research couldn't settle, and imported drafts."""
    out = []
    for kind in REVIEW_KINDS:
        for r in conn.execute(f"SELECT entity_uid, name, extra_json FROM {SPECS[kind].table} "
                              "WHERE enrichment_status = ? ORDER BY name", (REVIEW_STATUS,)):
            rec = parse_extra_json(r["extra_json"]).get("research") or {}
            out.append({"kind": kind, "entity_uid": r["entity_uid"], "name": r["name"],
                        "status": rec.get("status"), "why": rec.get("why"),
                        "venues": rec.get("venues", []),
                        "candidates": rec.get("candidates", [])})
    return out


def resolve(conn: sqlite3.Connection, kind: str, entity_uid: str,
            accept_url: Optional[str] = None) -> list[str]:
    """Operator decision on a reviewed entity. `accept_url` (one of the
    recorded candidates) fills blanks from that candidate's recorded
    fields; no URL rejects them all (not-found). Only rows in review can be
    resolved. Returns columns filled."""
    row = conn.execute(f"SELECT enrichment_status, extra_json FROM {SPECS[kind].table} "
                       "WHERE entity_uid = ?", (entity_uid,)).fetchone()
    if not row or row["enrichment_status"] != REVIEW_STATUS:
        raise ValueError(f"{kind} {entity_uid} is not waiting for review")
    rec = parse_extra_json(row["extra_json"]).get("research") or {}
    if accept_url is None:
        fill_blanks(conn, kind, entity_uid, {}, "not-found")
        _record(conn, kind, entity_uid, {**rec, "status": "operator-rejected", "filled": []})
        return []
    cand = next((c for c in rec.get("candidates", []) if c.get("url") == accept_url), None)
    if cand is None:
        raise ValueError(f"{accept_url} is not a recorded candidate for {entity_uid}")
    fields, _ = validate_payload(kind, cand.get("fields") or {})
    if listing_hosts.is_listing_url(fields.get("website")):
        del fields["website"]       # a draft's listing page is not its website
    filled = fill_blanks(conn, kind, entity_uid, fields, "enriched")
    has_text = any(k in fields for k in ("description", "bio"))
    _record(conn, kind, entity_uid, {**rec, "status": "operator-verified", "filled": filled,
                                     "description_source": accept_url if has_text else None})
    return filled
