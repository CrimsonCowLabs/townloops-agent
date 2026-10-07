"""Business discovery for this town: sweep the web for local businesses,
verify each candidate's page, and never propose one that is already live.

The sweeps and the page verifier are the `engine/discovery.py` kernel
(Firecrawl, no LLM). This module owns what the kernel deliberately doesn't:
the candidate store (`discovery_candidates`, kind 'business') and the dedup
against TownLoops, ported from the CMO's `townloops-business-discovery`
(`build_existing_index` / `already_in_townloops`): a candidate whose website
is a live business's website, or whose name is ≥0.85 similar to a live
business's name, is stored as `already-live` with that business's slug.

Candidate statuses:
  new           swept, waiting for verify
  verified      page checks out — proposed to the operator
  rejected      the verifier (or the operator) said no; never re-proposed
  already-live  matches a live business; never re-proposed
  approved      the operator said ship it — waiting for the promote run
  promoted      shipped as a business (`research_entity_uid`, `live_slug`)

A re-sweep only touches `new`/`verified` rows (they can still turn out to be
live); whatever else is decided stays decided. An operator decline
(`reject_reason` 'operator…') is remembered by name too: a later candidate
under another URL whose name matches it lands as `rejected`. Never writes to
TownLoops — promoting a candidate is engine/business_promote.py, after the
operator approves.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional
from urllib.parse import urlparse

from engine import discovery
from engine.derive_helpers import entity_richness, iso_utc_now, normalize_name


KIND = "business"
FUZZY_NAME_RATIO = 0.85
OPERATOR_REASON = "operator"
PROPOSED = ("new", "verified")
_PROPOSED_SQL = "(" + ", ".join(f"'{s}'" for s in PROPOSED) + ")"

# Hosts many businesses share: a website here identifies a business only by
# its exact page, never by the host.
SHARED_HOSTS = {
    "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
    "youtube.com", "linktr.ee", "sites.google.com", "google.com",
    "eventbrite.com", "yelp.com", "tripadvisor.com",
}

_GENERIC_TITLE_PARTS = {"home", "homepage", "home page", "welcome", "official site",
                        "official website", "index"}
_TITLE_SPLIT = re.compile(r"\s+[|\-–—:]\s+")


# --------------------------------------------------------------------------
# Live dedup
# --------------------------------------------------------------------------

@dataclass
class LiveMatch:
    slug: str
    name: str
    reason: str


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


class LiveIndex:
    """The town's live businesses, indexed for candidate dedup. Works for
    any live entity list; `kind` names it in match reasons."""

    def __init__(self, rows: list[dict], kind: str = KIND):
        self.rows = rows
        self.kind = kind
        self._by_url: dict[str, dict] = {}
        self._by_site: dict[tuple[str, str], dict] = {}   # (host, path) → row
        for r in sorted(rows, key=entity_richness):   # richest row wins a shared key
            site = r.get("website")
            if not site:
                continue
            canon = discovery.canonicalize_url(site)
            self._by_url[canon] = r
            host = _host(canon)
            if host and host not in SHARED_HOSTS:
                self._by_site[(host, urlparse(canon).path or "/")] = r

    @classmethod
    def build(cls, rows: Iterable[dict], kind: str = KIND) -> "LiveIndex":
        return cls([r for r in rows if r.get("slug")], kind)

    @classmethod
    def from_api(cls, client) -> "LiveIndex":
        return cls.build(client.paginate("businesses"))

    def match(self, canonical_url: str, name: Optional[str]) -> Optional[LiveMatch]:
        row = self._by_url.get(canonical_url)
        if row:
            return LiveMatch(row["slug"], row.get("name") or "",
                             f"url matches live {self.kind} website")
        row = self._site_match(canonical_url)
        if row:
            return LiveMatch(row["slug"], row.get("name") or "",
                             f"under live {self.kind} website {row.get('website')}")
        target = normalize_name(name or "")
        if not target:
            return None
        best, best_rank = None, None
        for r in self.rows:
            ratio = SequenceMatcher(None, target, normalize_name(r.get("name") or "")).ratio()
            if ratio < FUZZY_NAME_RATIO:
                continue
            rank = (ratio, entity_richness(r))
            if best_rank is None or rank > best_rank:
                best, best_rank = r, rank
        if best is None:
            return None
        return LiveMatch(best["slug"], best.get("name") or "",
                         f"name fuzzy-matches live {self.kind} {best.get('name')!r} "
                         f"({best_rank[0]:.2f})")


    def _site_match(self, canonical_url: str) -> Optional[dict]:
        """The live business whose website contains this page: same host, and
        the page sits under the website's path. A chain's per-location
        website (`aleworx.com/stateline`) covers only that location's pages."""
        host = _host(canonical_url)
        if host in SHARED_HOSTS:
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


def name_from_title(title: Optional[str]) -> Optional[str]:
    """A business name from a page title: 'Home | Alibi Ale Works' → 'Alibi
    Ale Works'. The first segment that isn't a generic page label."""
    for part in _TITLE_SPLIT.split(title or ""):
        part = part.strip()
        if part and part.lower() not in _GENERIC_TITLE_PARTS:
            return part[:120]
    return None


# --------------------------------------------------------------------------
# Candidate store
# --------------------------------------------------------------------------

def declined_match(conn: sqlite3.Connection, town_slug: str,
                   name: Optional[str]) -> Optional[dict]:
    """The candidate the operator declined whose name this one matches
    (normalized, ≥0.85) — a verifier rejection doesn't count."""
    target = normalize_name(name or "")
    if not target:
        return None
    for row in conn.execute(
        "SELECT candidate_uid, display_name FROM discovery_candidates "
        "WHERE town_slug = ? AND kind = ? AND status = 'rejected' "
        "AND (reject_reason = ? OR reject_reason LIKE ?)",
        (town_slug, KIND, OPERATOR_REASON, OPERATOR_REASON + ":%"),
    ).fetchall():
        other = normalize_name(row[1] or "")
        if other and SequenceMatcher(None, target, other).ratio() >= FUZZY_NAME_RATIO:
            return {"candidate_uid": row[0], "display_name": row[1]}
    return None


def declined_reason(d: dict) -> str:
    return f"operator declined {d['display_name']!r} earlier ({d['candidate_uid'][:12]})"


def candidate_uid(town_slug: str, canonical_url: str) -> str:
    return hashlib.sha1(f"{town_slug}|{canonical_url}|{KIND}".encode("utf-8")).hexdigest()


def _mark_live(conn: sqlite3.Connection, uid: str, m: LiveMatch, now: str) -> None:
    conn.execute(
        "UPDATE discovery_candidates SET status = 'already-live', live_slug = ?, "
        f"reject_reason = ?, last_seen_at = ? WHERE candidate_uid = ? AND status IN {_PROPOSED_SQL}",
        (m.slug, m.reason, now, uid),
    )


def record_sweep(conn: sqlite3.Connection, town_slug: str,
                 candidates: Iterable[discovery.Candidate], index: LiveIndex) -> dict:
    """Store swept candidates. New URLs are inserted (`new`, or
    `already-live` when they match a live business); URLs seen before only
    refresh `last_seen_at`, except a still-proposed one that is now live.
    A new URL whose name matches an operator decline is stored `rejected`."""
    counts = {"seen": 0, "inserted": 0, "already_live": 0, "declined": 0, "updated": 0}
    for c in candidates:
        counts["seen"] += 1
        now = iso_utc_now()
        uid = candidate_uid(town_slug, c.canonical_url)
        live = index.match(c.canonical_url, c.display_name)
        existing = conn.execute(
            "SELECT status, display_name FROM discovery_candidates WHERE candidate_uid = ?",
            (uid,),
        ).fetchone()
        if existing is None:
            declined = None if live else declined_match(conn, town_slug, c.display_name)
            status = "already-live" if live else "rejected" if declined else "new"
            reason = live.reason if live else declined_reason(declined) if declined else None
            conn.execute(
                """INSERT INTO discovery_candidates
                     (candidate_uid, town_slug, kind, target_category_slug, canonical_url,
                      display_name, source_signal, status, live_slug, reject_reason,
                      first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uid, town_slug, KIND, c.target_category_slug, c.canonical_url,
                 c.display_name, c.source_signal, status,
                 live.slug if live else None, reason, now, now),
            )
            counts["already_live" if live else "declined" if declined else "inserted"] += 1
            continue
        if existing[0] in PROPOSED:
            live = live or index.match(c.canonical_url, existing[1])
            if live:
                _mark_live(conn, uid, live, now)
                counts["already_live"] += 1
                continue
        conn.execute(
            "UPDATE discovery_candidates SET last_seen_at = ?, "
            "display_name = COALESCE(display_name, ?) WHERE candidate_uid = ?",
            (now, c.display_name, uid),
        )
        counts["updated"] += 1
    conn.commit()
    return counts


def pending_verify(conn: sqlite3.Connection, town_slug: str, limit: int) -> list[dict]:
    return _candidates(conn, """SELECT * FROM discovery_candidates
           WHERE town_slug = ? AND kind = ? AND status = 'new'
           ORDER BY first_seen_at, rowid LIMIT ?""", (town_slug, KIND, limit))


def approved_queue(conn: sqlite3.Connection, town_slug: str, limit: int) -> list[dict]:
    """Operator-approved candidates for the promote run: fewest promote
    attempts first, so ones that keep failing don't block the rest."""
    return _candidates(conn, """SELECT * FROM discovery_candidates
           WHERE town_slug = ? AND kind = ? AND status = 'approved'
           ORDER BY promote_attempts, last_seen_at, rowid LIMIT ?""", (town_slug, KIND, limit))


def record_verdict(conn: sqlite3.Connection, candidate: dict, verdict: dict,
                   index: LiveIndex) -> Optional[str]:
    """Store the verifier's verdict on one `new` candidate and return its new
    status — or None when it is no longer `new` (the operator decided while
    the verify ran; their call stands). A nameless (directory) candidate
    takes its name from the page title, and the live and operator-decline
    checks run again with it, since live may have grown since the sweep."""
    name = candidate.get("display_name") or name_from_title(verdict.get("title"))
    live = index.match(candidate["canonical_url"], name)
    declined = None if live else declined_match(conn, candidate["town_slug"], name)
    if live:
        status, live_slug, reason = "already-live", live.slug, live.reason
    elif declined:
        status, live_slug, reason = "rejected", None, declined_reason(declined)
    elif verdict.get("verified"):
        status, live_slug, reason = "verified", None, None
    else:
        status, live_slug = "rejected", None
        reason = "; ".join(verdict.get("reasons") or []) or "unspecified"
    cur = conn.execute(
        """UPDATE discovery_candidates
           SET status = ?, live_slug = ?, reject_reason = ?, display_name = ?,
               verify_json = ?, last_seen_at = ?
           WHERE candidate_uid = ? AND status = 'new'""",
        (status, live_slug, reason, name, json.dumps(verdict, ensure_ascii=False),
         iso_utc_now(), candidate["candidate_uid"]),
    )
    conn.commit()
    return status if cur.rowcount else None


def list_candidates(conn: sqlite3.Connection, town_slug: str,
                    status: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Candidates for the operator. Default: the proposed ones (verified
    first, then still-unverified); `status='all'` or one status to see the
    rest, e.g. what was deduped as `already-live`."""
    where, params = ["town_slug = ?", "kind = ?"], [town_slug, KIND]
    if status is None:
        where.append(f"status IN ({', '.join('?' * len(PROPOSED))})")
        params.extend(PROPOSED)
    elif status != "all":
        where.append("status = ?")
        params.append(status)
    return _candidates(conn, f"""SELECT * FROM discovery_candidates WHERE {' AND '.join(where)}
            ORDER BY CASE status WHEN 'verified' THEN 0 WHEN 'new' THEN 1 ELSE 2 END,
                     first_seen_at, rowid
            LIMIT ?""", (*params, limit))


def stats(conn: sqlite3.Connection, town_slug: str) -> dict:
    return dict(conn.execute(
        "SELECT status, COUNT(*) FROM discovery_candidates "
        "WHERE town_slug = ? AND kind = ? GROUP BY status ORDER BY status",
        (town_slug, KIND),
    ).fetchall())


def _candidates(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    """Rows as dicts (whatever the connection's row_factory), with
    `verify_json` decoded into `verify`."""
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        d["verify"] = json.loads(d.pop("verify_json") or "null")
        out.append(d)
    return out


# --------------------------------------------------------------------------
# Drivers
# --------------------------------------------------------------------------

def _business_profile(profile: dict) -> dict:
    return ((profile.get("discovery") or {}).get(KIND) or {})


def query_templates(profile: dict, town_name: str, region: str) -> list[str]:
    loc = f"{town_name}, {region}".strip(", ")
    out: list[str] = []
    for term in _business_profile(profile).get("category_query_terms") or []:
        out += [f"{term} in {loc}", f"best {term} {town_name}", f"local {term} {town_name}"]
    return out


def run_sweep(conn: sqlite3.Connection, client, profile: dict, town_slug: str,
              *, per_source: int = 25) -> dict:
    """Web search + seed directories (+ Facebook when the profile lists
    `facebook_terms`) from town-profile.yaml → `discovery.business`, deduped
    against live. The live list is read first: no Firecrawl spend when
    TownLoops can't answer."""
    index = LiveIndex.from_api(client)
    town_name = profile.get("name") or town_slug
    region = profile.get("region") or ""
    cfg = _business_profile(profile)
    errors: list[str] = []
    found: list[discovery.Candidate] = []
    templates = query_templates(profile, town_name, region)
    if templates:
        found += discovery.sweep_web_search(town_name, region, templates, kind=KIND,
                                            per_source=per_source, error_sink=errors)
    if cfg.get("seed_directories"):
        found += discovery.sweep_directories(cfg["seed_directories"], kind=KIND,
                                             per_source=per_source, error_sink=errors)
    if cfg.get("facebook_terms"):
        found += discovery.sweep_facebook(town_name, cfg["facebook_terms"], kind=KIND,
                                          per_source=per_source, error_sink=errors)
    counts = record_sweep(conn, town_slug, found, index)
    return {**counts, "live_businesses": len(index.rows), "errors": errors}


def run_verify(conn: sqlite3.Connection, client, profile: dict, town_slug: str,
               *, limit: int = 20) -> dict:
    """Verify up to `limit` `new` candidates. A verifier crash (Firecrawl
    down) leaves the candidate `new` for the next run rather than rejecting it."""
    index = LiveIndex.from_api(client)
    town_name = profile.get("name") or town_slug
    out = {"processed": 0, "verified": 0, "rejected": 0, "already_live": 0,
           "deferred": 0, "errors": []}
    for cand in pending_verify(conn, town_slug, limit):
        out["processed"] += 1
        try:
            verdict = discovery.verify_candidate(
                cand["canonical_url"], town_name=town_name,
                region=profile.get("region") or "",
                aliases=list(profile.get("name_aliases") or []),
            )
        except Exception as e:
            out["deferred"] += 1
            out["errors"].append(f"{cand['canonical_url']}: {type(e).__name__}: {e}")
            continue
        status = record_verdict(conn, cand, verdict, index)
        if status:
            out[status.replace("-", "_")] += 1
    return out
