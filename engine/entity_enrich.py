"""Entity enrichment — resolve a venue/business/community named on an event to
a local row (local match → live mirror → new shell) and fill its blank fields
from the entity's own website, under a lookup budget.

Never writes to TownLoops. Live rows are mirrored locally; an image found for
a live row without one is staged as a proposal (`image-proposed`) for the
operator, never filled into other fields.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Literal, Optional
from urllib.parse import urlparse

from engine.derive_helpers import (
    best_name_match, insert_entity, iso_utc_now, mint_entity_uid, mirror_live_entity,
    names_match, normalize_name,
)


TABLES = {"venue": "venues", "business": "businesses", "community": "communities"}
# Fields research_url() can fill on a local row (fill-blanks only).
ENRICH_FIELDS = ("description", "address", "website", "phone", "image_path",
                 "image_alt", "instagram", "facebook", "tiktok", "twitter",
                 "youtube", "linkedin")


class Budget:
    """Cap on Firecrawl search/scrape calls; each call takes one."""

    def __init__(self, n: int):
        self.left = n

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


@dataclass
class Resolution:
    entity_uid: str
    outcome: Literal["local", "live", "created"]
    filled: list[str] = field(default_factory=list)
    # Set when a live row without an image got one staged locally.
    image_proposal: Optional[dict] = None


def _pick_site(name: str, results: list[dict]) -> Optional[str]:
    """First search hit that is plausibly the entity's own site."""
    from engine.discovery import host_blocked
    compact = normalize_name(name).replace(" ", "")
    for r in results:
        url = r.get("url") or ""
        if not url or host_blocked(url, allow_gov=True):
            continue
        host = (urlparse(url).hostname or "").replace("-", "").replace(".", "")
        title = (r.get("title") or "").split("|")[0].split(" - ")[0]
        if names_match(name, title) or (len(compact) >= 5 and compact[:12] in host):
            return url
    return None


def research_entity(name: str, website: Optional[str], town: str, budget: Budget) -> dict:
    """Find (search, if no website) and scrape the entity's site. Returns a
    payload of ENRICH_FIELDS; {} when nothing was found or the budget ran out."""
    from engine import discovery, research
    url = website
    if not url:
        if not budget.take():
            return {}
        url = _pick_site(name, discovery.firecrawl_search(f'"{name}" {town}', limit=5))
        if not url:
            return {}
    if not budget.take():
        return {}
    res = research.research_url(url, use_firecrawl=True)
    payload = {k: v for k, v in res.payload.items() if k in ENRICH_FIELDS and v}
    if res.fetched_from:
        payload.setdefault("website", website or res.fetched_from[0])
    return payload


def fill_blanks(conn: sqlite3.Connection, kind: str, entity_uid: str, payload: dict,
                status: str) -> list[str]:
    """Write payload values into the row's empty columns only, and record
    `status` as its enrichment_status. Returns the columns filled."""
    table = TABLES[kind]
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info('{table}')")}
    row = dict(conn.execute(f"SELECT * FROM {table} WHERE entity_uid = ?", (entity_uid,)).fetchone())
    filled = [k for k, v in payload.items() if k in cols and v and not row.get(k)]
    sets = [f"{k} = ?" for k in filled]
    vals = [payload[k] for k in filled]
    if "enrichment_status" in cols:
        sets.append("enrichment_status = ?")
        vals.append(status)
    if "enriched_at" in cols:
        sets.append("enriched_at = ?")
        vals.append(iso_utc_now())
    if filled:
        sets.append("last_modified_at = ?")
        vals.append(iso_utc_now())
    if sets:
        conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE entity_uid = ?",
                     (*vals, entity_uid))
    return filled


def resolve_entity(conn: sqlite3.Connection, kind: str, name: str, event: dict,
                   live_rows: list[dict], town: str, budget: Budget) -> Resolution:
    """Resolve `name` (a `kind` named on `event`) to a local entity_uid:

    - local match → enriched once if it has no image (live mirrors only get
      an image proposal);
    - live match (from `live_rows`) → mirrored locally; if the live row has
      no image, one found on its site is staged as a proposal;
    - otherwise → a new derived shell, enriched from a site search.
    """
    table = TABLES[kind]
    local_rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    hit = best_name_match(name, local_rows)
    if hit:
        filled: list[str] = []
        if not hit.get("image_path") and hit.get("enrichment_status") not in ("enriched", "not-found", "image-proposed"):
            payload = research_entity(hit["name"], hit.get("website"), town, budget)
            if payload:
                status = "image-proposed" if hit.get("source_kind") == "live" else "enriched"
                filled = fill_blanks(conn, kind, hit["entity_uid"], payload, status)
        return Resolution(hit["entity_uid"], "local", filled)

    live_hit = best_name_match(name, live_rows)
    if live_hit:
        entity_uid = mirror_live_entity(conn, kind, live_hit)
        if live_hit.get("image_path"):
            return Resolution(entity_uid, "live")
        payload = research_entity(live_hit["name"], live_hit.get("website"), town, budget)
        if not payload.get("image_path"):
            return Resolution(entity_uid, "live")
        filled = fill_blanks(conn, kind, entity_uid,
                             {k: payload[k] for k in ("image_path", "image_alt") if payload.get(k)},
                             "image-proposed")
        return Resolution(entity_uid, "live", filled,
                          {"kind": kind, "slug": live_hit["slug"], "image_path": payload["image_path"]})

    entity_uid = mint_entity_uid(kind, name, event["event_uid"])
    fields = {
        "name": name.strip(), "source": f"derived:event:{event['event_uid'][:12]}",
        "source_kind": "derived", "derived_from_event_uid": event["event_uid"],
        "derivation_confidence": 70, "enrichment_status": "pending",
        "category_slug": event.get("category_slug"),
    }
    if kind == "venue":
        fields["address"] = event.get("location") or event.get("location_raw")
    if kind == "community":
        fields["kind"] = "group"
    insert_entity(conn, table, entity_uid, fields)
    payload = research_entity(name, None, town, budget)
    filled = fill_blanks(conn, kind, entity_uid, payload, "enriched" if payload else "not-found")
    return Resolution(entity_uid, "created", filled)
