"""Organisation discovery for this town: sweep the web for local businesses,
community groups and non-profits (`KINDS`), verify each candidate's page,
and never propose one that is already on TownLoops.

The sweeps and the page verifier are the `engine/discovery.py` kernel
(Firecrawl, no LLM). This module owns what the kernel deliberately doesn't:
the candidate store (`discovery_candidates`, kind 'business') and the dedup
against TownLoops, ported from the CMO's `townloops-business-discovery`
(`build_existing_index` / `already_in_townloops`): a candidate whose website
is a live business's website, or whose name is ≥0.85 similar to a live
business's name, is stored as `already-live` with that business's slug. The
same checks run against the town's live venues (a food hall like Cascade
Kitchens is a venue, never a business or community): a venue match is
`already-live` too, its reason naming the venue slug.

Candidate statuses:
  new           swept, waiting for verify
  verified      page checks out — proposed to the operator
  rejected      the verifier (or the operator) said no; never re-proposed
  already-live  matches a live business (or venue); never re-proposed
  approved      the operator said ship it — waiting for the promote run
  promoted      shipped as a business (`research_entity_uid`, `live_slug`)

Every function takes the candidate `kind` ('business' by default); a
community group and a non-profit both ship as a community (a non-profit with
community `kind` 'nonprofit'). A live match counts whatever its TownLoops
status: a pending one is `already-live` (already submitted), a rejected one
is `rejected` with a `townloops` reason.

A re-sweep only touches `new`/`verified` rows (they can still turn out to be
live); whatever else is decided stays decided. An operator decline
(`reject_reason` 'operator…') is remembered by name too: a later candidate
under another URL whose name matches it lands as `rejected`; so does a
TownLoops moderator's rejection (`reject_reason` 'townloops…',
engine/moderation). Never writes to
TownLoops — promoting a candidate is engine/business_promote.py, after the
operator approves.

A stored verdict is the verifier's whole return value (its `fetched_at` is
when it ran); the CMO import marks a copied one `imported_from`.
Autonomous approval trusts only a recent verdict of this verifier — any
other is re-verified first (`reverifier`, engine/autonomy.judge_candidate).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

from engine import discovery, listing_hosts
from engine.derive_helpers import entity_richness, iso_utc_now, normalize_name


KIND = "business"
# Candidate kind → the TownLoops resource it ships as.
KINDS = {"business": "businesses", "community": "communities", "nonprofit": "communities"}
FUZZY_NAME_RATIO = 0.85
OPERATOR_REASON = "operator"
TOWNLOOPS_REASON = "townloops"
# Rejections remembered by name across URLs.
_REMEMBERED = (OPERATOR_REASON, TOWNLOOPS_REASON)
PROPOSED = ("new", "verified")
_PROPOSED_SQL = "(" + ", ".join(f"'{s}'" for s in PROPOSED) + ")"

# Hosts many businesses share: a website here identifies a business only by
# its exact page, never by the host. Listing hosts (engine.listing_hosts)
# are shared too — see _shared_host.
SHARED_HOSTS = {
    "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
    "youtube.com", "linktr.ee", "sites.google.com", "google.com",
}

# --------------------------------------------------------------------------
# Live dedup
# --------------------------------------------------------------------------

@dataclass
class LiveMatch:
    slug: str
    name: str
    reason: str
    status: Optional[str] = None     # the live row's TownLoops status, when it has one


def _shared_host(host: str) -> bool:
    return host in SHARED_HOSTS or listing_hosts.is_listing_host(host)


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


class LiveIndex:
    """The town's live businesses, indexed for candidate dedup. Works for
    any live entity list; `kind` names it in match reasons. `venues`, when
    given, is checked after the rows themselves: a candidate that is a live
    venue is never proposed either."""

    def __init__(self, rows: list[dict], kind: str = KIND,
                 venues: Optional["LiveIndex"] = None):
        self.rows = rows
        self.kind = kind
        self.venues = venues
        self._by_url: dict[str, dict] = {}
        self._by_site: dict[tuple[str, str], dict] = {}   # (host, path) → row
        for r in sorted(rows, key=entity_richness):   # richest row wins a shared key
            site = r.get("website")
            if not site:
                continue
            canon = discovery.canonicalize_url(site)
            self._by_url[canon] = r
            host = _host(canon)
            if host and not _shared_host(host):
                self._by_site[(host, urlparse(canon).path or "/")] = r

    @classmethod
    def build(cls, rows: Iterable[dict], kind: str = KIND,
              venues: Optional[Iterable[dict]] = None) -> "LiveIndex":
        # A venue TownLoops rejected isn't a live venue.
        vindex = None if venues is None else cls(
            [v for v in venues if v.get("slug") and v.get("status") != "rejected"], "venue")
        return cls([r for r in rows if r.get("slug")], kind, vindex)

    @classmethod
    def from_api(cls, client, kind: str = KIND) -> "LiveIndex":
        return cls.build(client.paginate(KINDS[kind]), "business" if kind == KIND else "community",
                         venues=client.paginate("venues"))

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
        row = self._by_url.get(canonical_url)
        if row:
            return LiveMatch(row["slug"], row.get("name") or "",
                             f"url matches live {self.kind} website", row.get("status"))
        row = self._site_match(canonical_url)
        if row:
            return LiveMatch(row["slug"], row.get("name") or "",
                             f"under live {self.kind} website {row.get('website')}",
                             row.get("status"))
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
                         f"({best_rank[0]:.2f})", best.get("status"))


    def _site_match(self, canonical_url: str) -> Optional[dict]:
        """The live business whose website contains this page: same host, and
        the page sits under the website's path. A chain's per-location
        website (`aleworx.com/stateline`) covers only that location's pages."""
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


# --------------------------------------------------------------------------
# Candidate store
# --------------------------------------------------------------------------

def declined_match(conn: sqlite3.Connection, town_slug: str,
                   name: Optional[str], kind: str = KIND) -> Optional[dict]:
    """The candidate the operator declined — or the TownLoops moderator
    rejected — whose name this one matches (normalized, ≥0.85). A verifier
    rejection doesn't count."""
    target = normalize_name(name or "")
    if not target:
        return None
    # A community group and a non-profit are one TownLoops resource.
    kinds = [k for k, r in KINDS.items() if r == KINDS.get(kind, KINDS[KIND])]
    for row in conn.execute(
        "SELECT candidate_uid, display_name, reject_reason FROM discovery_candidates "
        f"WHERE town_slug = ? AND kind IN ({', '.join('?' * len(kinds))}) AND status = 'rejected' "
        "AND (reject_reason IN (?, ?) OR reject_reason LIKE ? OR reject_reason LIKE ?)",
        (town_slug, *kinds, *_REMEMBERED, *(r + ":%" for r in _REMEMBERED)),
    ).fetchall():
        other = normalize_name(row[1] or "")
        if other and SequenceMatcher(None, target, other).ratio() >= FUZZY_NAME_RATIO:
            return {"candidate_uid": row[0], "display_name": row[1],
                    "by": TOWNLOOPS_REASON if (row[2] or "").startswith(TOWNLOOPS_REASON)
                    else OPERATOR_REASON}
    return None


def declined_reason(d: dict) -> str:
    who = "TownLoops rejected" if d.get("by") == TOWNLOOPS_REASON else "operator declined"
    return f"{who} {d['display_name']!r} earlier ({d['candidate_uid'][:12]})"


def candidate_uid(town_slug: str, canonical_url: str, kind: str = KIND) -> str:
    return hashlib.sha1(f"{town_slug}|{canonical_url}|{kind}".encode("utf-8")).hexdigest()


def live_status(m: LiveMatch) -> tuple[str, str]:
    """(candidate status, reason) for a live match: rejected on TownLoops is
    remembered as a rejection, anything else is already live."""
    if m.status == "rejected":
        return "rejected", f"{TOWNLOOPS_REASON}: rejected on TownLoops as {m.slug}"
    return "already-live", m.reason


def _mark_live(conn: sqlite3.Connection, uid: str, m: LiveMatch, now: str) -> None:
    status, reason = live_status(m)
    conn.execute(
        "UPDATE discovery_candidates SET status = ?, live_slug = ?, "
        f"reject_reason = ?, last_seen_at = ? WHERE candidate_uid = ? AND status IN {_PROPOSED_SQL}",
        (status, m.slug, reason, now, uid),
    )


def record_sweep(conn: sqlite3.Connection, town_slug: str,
                 candidates: Iterable[discovery.Candidate], index: LiveIndex,
                 kind: str = KIND) -> dict:
    """Store swept candidates. New URLs are inserted (`new`, or
    `already-live` when they match a live business or venue); URLs seen before only
    refresh `last_seen_at`, except a still-proposed one that is now live.
    A new URL whose name matches an operator decline is stored `rejected`."""
    counts = {"seen": 0, "inserted": 0, "already_live": 0, "declined": 0, "updated": 0}
    for c in candidates:
        counts["seen"] += 1
        now = iso_utc_now()
        uid = candidate_uid(town_slug, c.canonical_url, kind)
        live = index.match(c.canonical_url, c.display_name)
        existing = conn.execute(
            "SELECT status, display_name FROM discovery_candidates WHERE candidate_uid = ?",
            (uid,),
        ).fetchone()
        if existing is None:
            declined = None if live else declined_match(conn, town_slug, c.display_name, kind)
            if live:
                status, reason = live_status(live)
            else:
                status = "rejected" if declined else "new"
                reason = declined_reason(declined) if declined else None
            conn.execute(
                """INSERT INTO discovery_candidates
                     (candidate_uid, town_slug, kind, target_category_slug, canonical_url,
                      display_name, source_signal, status, live_slug, reject_reason,
                      first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uid, town_slug, kind, c.target_category_slug, c.canonical_url,
                 c.display_name, c.source_signal, status,
                 live.slug if live else None, reason, now, now),
            )
            counts["declined" if declined or status == "rejected" else
                   "already_live" if live else "inserted"] += 1
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


def pending_verify(conn: sqlite3.Connection, town_slug: str, limit: int,
                   kind: str = KIND) -> list[dict]:
    return _candidates(conn, """SELECT * FROM discovery_candidates
           WHERE town_slug = ? AND kind = ? AND status = 'new'
           ORDER BY first_seen_at, rowid LIMIT ?""", (town_slug, kind, limit))


def approved_queue(conn: sqlite3.Connection, town_slug: str, limit: int,
                   kind: str = KIND) -> list[dict]:
    """Operator-approved candidates for the promote run: fewest promote
    attempts first, so ones that keep failing don't block the rest."""
    return _candidates(conn, """SELECT * FROM discovery_candidates
           WHERE town_slug = ? AND kind = ? AND status = 'approved'
           ORDER BY promote_attempts, last_seen_at, rowid LIMIT ?""", (town_slug, kind, limit))


def record_verdict(conn: sqlite3.Connection, candidate: dict, verdict: dict,
                   index: LiveIndex, *, from_status: str = "new") -> Optional[str]:
    """Store the verifier's verdict on one `new` candidate (a re-verified
    one: `from_status='verified'`) and return its new status — or None when
    it has moved on (the operator decided while the verify ran; their call
    stands). The verdict replaces the stored one whole. A nameless (directory) candidate
    takes its name from the page title, and the live and operator-decline
    checks run again with it, since live may have grown since the sweep."""
    name = candidate.get("display_name") or discovery.name_from_title(verdict.get("title"))
    live = index.match(candidate["canonical_url"], name)
    declined = None if live else declined_match(conn, candidate["town_slug"], name,
                                                candidate.get("kind") or KIND)
    if live:
        (status, reason), live_slug = live_status(live), live.slug
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
           WHERE candidate_uid = ? AND status = ?""",
        (status, live_slug, reason, name, json.dumps(verdict, ensure_ascii=False),
         iso_utc_now(), candidate["candidate_uid"], from_status),
    )
    conn.commit()
    return status if cur.rowcount else None


def get_candidate(conn: sqlite3.Connection, uid: str) -> Optional[dict]:
    rows = _candidates(conn, "SELECT * FROM discovery_candidates WHERE candidate_uid = ?",
                       (uid,))
    return rows[0] if rows else None


def list_candidates(conn: sqlite3.Connection, town_slug: str,
                    status: Optional[str] = None, limit: int = 50,
                    kind: str = KIND) -> list[dict]:
    """Candidates for the operator. Default: the proposed ones (verified
    first, then still-unverified); `status='all'` or one status to see the
    rest, e.g. what was deduped as `already-live`."""
    where, params = ["town_slug = ?", "kind = ?"], [town_slug, kind]
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


def stats(conn: sqlite3.Connection, town_slug: str, kind: str = KIND) -> dict:
    return dict(conn.execute(
        "SELECT status, COUNT(*) FROM discovery_candidates "
        "WHERE town_slug = ? AND kind = ? GROUP BY status ORDER BY status",
        (town_slug, kind),
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

def _kind_profile(profile: dict, kind: str = KIND) -> dict:
    return ((profile.get("discovery") or {}).get(kind) or {})


def _business_profile(profile: dict) -> dict:
    return _kind_profile(profile, KIND)


def configured_kinds(profile: dict) -> list[str]:
    """The kinds town-profile.yaml → `discovery` gives sweep sources for."""
    return [k for k in KINDS if any(_kind_profile(profile, k).get(src) for src in (
        "category_query_terms", "query_groups", "seed_directories", "facebook_terms"))]


# Search shapes that land on an organisation's own site, never a roundup:
# no "best"/"top"/"in {town}" (those rank listicles first). One shape adds
# what an own site carries for its kind; the other asks for the site itself.
OWN_SITE_SIGNAL = {"business": "hours contact", "community": "about us", "nonprofit": "donate"}


def query_templates(profile: dict, town_name: str, region: str, kind: str = KIND) -> list[str]:
    loc = f"{town_name}, {region}".strip(", ")
    cfg = _kind_profile(profile, kind)
    terms = list(cfg.get("category_query_terms") or [])
    for group in cfg.get("query_groups") or []:
        terms += list((group or {}).get("terms") or [])
    return [q for term in terms for q in (
        f"{term} {loc} {OWN_SITE_SIGNAL[kind]}", f"{term} {town_name} official website")]


def seed_directory_hosts(profile: dict) -> list[str]:
    """Every kind's seed directory hosts: a directory is never a candidate of
    any kind."""
    return sorted({listing_hosts.bare_host(u) for k in KINDS
                   for u in _kind_profile(profile, k).get("seed_directories") or []})


def run_sweep(conn: sqlite3.Connection, client, profile: dict, town_slug: str,
              *, per_source: int = 25, kind: str = KIND) -> dict:
    """Web search + seed directories (+ Facebook when the profile lists
    `facebook_terms`) from town-profile.yaml → `discovery.business`, deduped
    against live. The live list is read first: no Firecrawl spend when
    TownLoops can't answer."""
    index = LiveIndex.from_api(client, kind)
    town_name = profile.get("name") or town_slug
    region = profile.get("region") or ""
    cfg = _kind_profile(profile, kind)
    errors: list[str] = []
    found: list[discovery.Candidate] = []
    templates = query_templates(profile, town_name, region, kind)
    if templates:
        found += discovery.sweep_web_search(town_name, region, templates, kind=kind,
                                            per_source=per_source,
                                            # A government department is a community's.
                                            allow_gov=kind != KIND,
                                            directory_hosts=seed_directory_hosts(profile),
                                            error_sink=errors)
    if cfg.get("seed_directories"):
        found += discovery.sweep_directories(cfg["seed_directories"], kind=kind,
                                             per_source=per_source, error_sink=errors)
    if cfg.get("facebook_terms"):
        found += discovery.sweep_facebook(town_name, cfg["facebook_terms"], kind=kind,
                                          per_source=per_source, error_sink=errors)
    counts = record_sweep(conn, town_slug, found, index, kind)
    live_key = "live_businesses" if kind == KIND else "live_communities"
    return {**counts, live_key: len(index.rows),
            "live_venues": len(index.venues.rows) if index.venues else 0, "errors": errors}


def _verify(cand: dict, profile: dict, town_slug: str, kind: str) -> dict:
    return discovery.verify_candidate(
        cand["canonical_url"], town_name=profile.get("name") or town_slug,
        region=profile.get("region") or "",
        aliases=list(profile.get("name_aliases") or []),
        **({} if kind == KIND else {"kind": kind}),
        name=cand.get("display_name"),
        directory_hosts=seed_directory_hosts(profile),
    )


def run_verify(conn: sqlite3.Connection, client, profile: dict, town_slug: str,
               *, limit: int = 20, kind: str = KIND) -> dict:
    """Verify up to `limit` `new` candidates. A verifier crash (Firecrawl
    down) leaves the candidate `new` for the next run rather than rejecting it."""
    index = LiveIndex.from_api(client, kind)
    out = {"processed": 0, "verified": 0, "rejected": 0, "already_live": 0,
           "deferred": 0, "errors": []}
    for cand in pending_verify(conn, town_slug, limit, kind):
        out["processed"] += 1
        try:
            verdict = _verify(cand, profile, town_slug, kind)
        except Exception as e:
            out["deferred"] += 1
            out["errors"].append(f"{cand['canonical_url']}: {type(e).__name__}: {e}")
            continue
        status = record_verdict(conn, cand, verdict, index)
        if status:
            out[status.replace("-", "_")] += 1
    return out


def reverifier(client, profile: dict, town_slug: str,
               kind: str = KIND) -> Callable[[sqlite3.Connection, dict], Optional[str]]:
    """Re-verify one `verified` candidate whose verdict can't be trusted as
    it stands (engine/autonomy.REVERIFY) with the current verifier, and
    store the fresh verdict → its new status (verified / rejected /
    already-live), None when it moved on meanwhile. An imported research
    draft (`verify.draft`) carries over. A verifier crash raises and leaves
    the candidate as it was. The live list is read on first use."""
    index: list[LiveIndex] = []

    def run(conn: sqlite3.Connection, cand: dict) -> Optional[str]:
        verdict = _verify(cand, profile, town_slug, kind)
        if draft := (cand.get("verify") or {}).get("draft"):
            verdict = {**verdict, "draft": draft}
        if not index:
            index.append(LiveIndex.from_api(client, kind))
        return record_verdict(conn, cand, verdict, index[0], from_status="verified")
    return run
