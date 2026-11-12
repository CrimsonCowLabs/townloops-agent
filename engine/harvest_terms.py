"""Organisation-harvest search terms from what the town is missing
(business-discovery `harvest`, issue #64).

The weekly harvest searched the profile's same ten terms every week and
found nothing new after the first. The daily harvest's terms for a
candidate kind come from three pools, in this priority:

1. **Hosts the town's events name with no entity** — `events.hosting_group`
   with no derived host (business, or community when the name reads as one:
   a library, a foundation, a city), `venue_name`/`venue_raw` with no derived
   venue (venue). A name already live (the run's live index: website /
   name ≥0.85), already a local entity, or already a candidate of any status
   is skipped. Busiest first.
2. **The profile's terms** (`discovery.<kind>` `category_query_terms` and
   `query_groups`).
3. **Categories thin on live** — each town category (the snapshot) with
   fewer than `THIN_LIVE` live rows of the kind: the keywords that point to
   it (engine/entity_classify.hints_for), from the concepts that kind can be
   (`GAP_CONCEPTS`: a restaurant is a business, a library a community, an
   amphitheatre a venue). Not for non-profits.

A run takes `limit` terms a kind, least recently searched first (never
searched before anything), so successive days search something new and
everything comes round again. `discovery_checks` (`term:<kind>:<term>`)
remembers when each was searched.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from difflib import SequenceMatcher
from typing import Iterable

from engine import categories, entity_classify
from engine.derive_helpers import normalize_name
from engine.discovery_checks import checked_at, mark

# A category with fewer live rows of the kind than this is thin.
THIN_LIVE = 3
# The classifier concepts (engine/entity_classify) each kind's gap terms
# come from.
GAP_CONCEPTS = {
    "business": ("food-and-drink", "outdoor-recreation", "family", "lodging", "shopping",
                 "wellness", "arts", "live-music"),
    "community": ("community",),
    "venue": ("live-music", "arts", "outdoor-recreation", "family"),
}
_FUZZY = 0.85
# Event "hosts"/"venues" that name no organisation.
_NOT_A_NAME = re.compile(r"^(?:online|virtual|zoom|tba|tbd|various|multiple|see .*|n/?a)$"
                         r"|\bzoom\b|\bonline\b|\bvirtual\b", re.IGNORECASE)
_COMMUNITY = re.compile(r"\b(?:library|foundation|non-?profit|association|society|club|church"
                        r"|rotary|city of|county|town of|district|council|alliance|league"
                        r"|friends of|coalition|guild)\b", re.IGNORECASE)


def _key(kind: str, term: str) -> str:
    return f"term:{kind}:{term.strip().lower()}"


def _known_names(conn: sqlite3.Connection) -> list[str]:
    names = []
    for table in ("venues", "businesses", "communities"):
        names += [r[0] for r in conn.execute(f"SELECT name FROM {table} WHERE name IS NOT NULL")]
    names += [r[0] for r in conn.execute(
        "SELECT display_name FROM discovery_candidates WHERE display_name IS NOT NULL")]
    return [n for n in (normalize_name(x) for x in names) if n]


def _is_known(name: str, known: list[str], index) -> bool:
    target = normalize_name(name)
    if any(SequenceMatcher(None, target, k).ratio() >= _FUZZY for k in known):
        return True
    return bool(index is not None and index.match("", name))


def host_terms(conn: sqlite3.Connection, kind: str, index=None) -> list[str]:
    """Names events give their host (venue, for kind `venue`) that no
    entity, live row or candidate has — busiest first."""
    if kind == "venue":
        sql = ("SELECT COALESCE(NULLIF(TRIM(venue_name), ''), TRIM(venue_raw)) FROM events "
               "WHERE derived_venue_uid IS NULL")
    elif kind in ("business", "community"):
        sql = "SELECT TRIM(hosting_group) FROM events WHERE derived_host_uid IS NULL"
    else:
        return []
    counts = Counter(n for (n,) in conn.execute(sql)
                     if n and len(n) >= 4 and not _NOT_A_NAME.search(n))
    known = _known_names(conn)
    out = []
    for name, _ in counts.most_common():
        if kind != "venue" and (kind == "community") != bool(_COMMUNITY.search(name)):
            continue
        if not _is_known(name, known, index):
            out.append(name)
    return out


def gap_terms(kind: str, live_rows: Iterable[dict]) -> list[str]:
    """Keywords of the town's categories with fewer than THIN_LIVE live rows
    of `kind` — thinnest first."""
    concepts = GAP_CONCEPTS.get(kind)
    if not concepts:
        return []
    slugs = [c["slug"] for c in categories.town_categories()]
    have = Counter(r.get("category_slug") for r in live_rows
                   if r.get("status") != "rejected" and r.get("category_slug"))
    out = []
    for slug in sorted(slugs, key=lambda s: (have[s], s)):
        if have[slug] < THIN_LIVE:
            out += entity_classify.hints_for(slug, slugs, concepts)
    return out


def pool(conn: sqlite3.Connection, kind: str, profile_terms: list[str], index=None) -> list[str]:
    """Every term the kind could search today, in priority order, deduped."""
    seen, out = set(), []
    live_rows = index.rows if index is not None else []
    for term in (*host_terms(conn, kind, index), *profile_terms, *gap_terms(kind, live_rows)):
        k = term.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(term.strip())
    return out


def pick(conn: sqlite3.Connection, kind: str, terms: list[str], limit: int) -> list[str]:
    """Today's `limit` terms: least recently searched first, priority order
    breaking ties."""
    ranked = sorted(enumerate(terms), key=lambda it: (checked_at(conn, _key(kind, it[1])) or "",
                                                       it[0]))
    return [t for _, t in ranked[:max(limit, 0)]]


def mark_searched(conn: sqlite3.Connection, kind: str, terms: Iterable[str]) -> None:
    for term in terms:
        mark(conn, _key(kind, term), "searched")
