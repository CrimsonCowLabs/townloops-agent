"""Read-only audit of venues, businesses and communities whose `website` is
a directory/listing page (`engine/listing_hosts.is_listing_url` — the one
check; seen on SLT: a business whose website was its restaurantji page).

Each local row is paired with a live row from the town's live listing:
first by its `townloops_slug`, then by `extra_json.live_id`, and only then
by a GET of the slug that follows a curator's merge to the survivor
(`live_lookup.find_live_entity`) — the local row is not re-pointed. Rows
filed under another town (`entity_sync.is_cross_town`) aren't paired: our
town-scoped reads can't see them. A live row no local row pairs with
stands alone.

One finding per local row (or unpaired live row), its `side` saying which
side holds a listing URL: `local`, `live` or `both`. Both websites are
given as stored (`None` where that side has no row), and `url` is the
listing one — live's when both are listings, even if they differ.

- A GET that fails other than with a 404 leaves the row `live_unread`: its
  live side is unknown (not "no live row"), and the row is also returned in
  `unverified`. Its live counterpart may still list under another slug as a
  separate `live` finding. A cleanup must not act on an unread row.
- Several local rows on one live row (duplicates, or rows merged into one
  survivor) count the live side once: it belongs to the primary — paired by
  slug before live_id before merge, then lowest `entity_uid` — and the
  others are judged on their local website alone, with `duplicate_of`
  naming the primary.

Only LIST/GET calls and SELECTs. The listing-website cleanup works from
`find_listing_websites`' rows.
"""

from __future__ import annotations

import sqlite3
from typing import NamedTuple, Optional

from engine.derive_helpers import dict_rows as _rows, parse_extra_json
from engine.entity_sync import RESOURCES, is_cross_town
from engine.listing_hosts import is_listing_url


# The kinds with a website the audit covers: kind → resource / table, report order.
KINDS = {k: RESOURCES[k] for k in ("venue", "business", "community")}


# How a local row found its live row, best first: the primary of several.
_PAIRED_BY = ("slug", "live_id", "merge")


class ListingWebsites(NamedTuple):
    rows: list[dict]
    unverified: list[dict]    # {kind, entity_uid, live_slug, error}: live side unread


def audit_listing_websites(conn: sqlite3.Connection, client, *, limit: int = 200) -> dict:
    """find_listing_websites, summarised: uncapped counts per side and kind,
    the first `limit` rows, and every unverified row."""
    found = find_listing_websites(conn, client)
    counts = {"local": 0, "live": 0, "both": 0}
    by_kind = dict.fromkeys(KINDS, 0)
    for r in found.rows:
        counts[r["side"]] += 1
        by_kind[r["kind"]] += 1
    return {"total": len(found.rows), "counts": counts, "by_kind": by_kind,
            "rows": found.rows[:limit], "unverified": found.unverified}


def find_listing_websites(conn: sqlite3.Connection, client) -> ListingWebsites:
    """Every venue/business/community with a listing-page website, locally,
    live or both (see module doc), ordered by kind then name. Each row:

        {"kind", "name", "side": "local"|"live"|"both", "url",
         "entity_uid" (None: no local row), "live_slug", "live_id" (None: not
         paired live), "local_website", "live_website",
         "live_unread" (True: the live side couldn't be read),
         "duplicate_of" (the primary's entity_uid when it shares a live row)}
    """
    rows, unverified = [], []
    for kind, resource in KINDS.items():
        live_rows = list(client.paginate(resource))
        pairs, kind_unverified = _pair(conn, client, resource, live_rows)
        for u in kind_unverified:
            unverified.append({"kind": kind, **u})
        primary: dict[str, dict] = {}
        for p in sorted((p for p in pairs if p["live"] is not None),
                        key=lambda p: (_PAIRED_BY.index(p["by"]), p["local"]["entity_uid"])):
            primary.setdefault(p["live"].get("slug"), p)
        for p in pairs:
            first = primary.get(p["live"].get("slug")) if p["live"] is not None else None
            dup = first["local"]["entity_uid"] if first is not None and first is not p else None
            rows.append(_finding(kind, p["local"], p["live"], live_counted=dup is None,
                                 live_unread=p["unread"], duplicate_of=dup))
        for live in live_rows:
            if live.get("slug") not in primary:
                rows.append(_finding(kind, None, live))
    order = list(KINDS)
    rows = [r for r in rows if r]
    rows.sort(key=lambda r: (order.index(r["kind"]), (r["name"] or "").lower(),
                             r["live_slug"] or "", r["entity_uid"] or ""))
    return ListingWebsites(rows, unverified)


def _pair(conn: sqlite3.Connection, client, resource: str,
          live_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Each local row with its live row: [{local, live, by, unread}], plus
    the rows whose GET failed."""
    from engine.live_lookup import find_live_entity
    by_slug = {r.get("slug"): r for r in live_rows if r.get("slug")}
    by_id = {r.get("id"): r for r in live_rows if r.get("id") is not None}
    pairs, unverified = [], []
    for row in _rows(conn, f"SELECT entity_uid, name, website, townloops_slug, extra_json "
                           f"FROM {resource} ORDER BY entity_uid"):
        slug = row["townloops_slug"]
        live, by, unread = None, None, False
        if not is_cross_town(row):
            live_id = parse_extra_json(row["extra_json"]).get("live_id")
            if slug and slug in by_slug:
                live, by = by_slug[slug], "slug"
            elif live_id is not None and live_id in by_id:
                live, by = by_id[live_id], "live_id"
            elif slug:
                try:
                    live, by = find_live_entity(client, resource, slug), "merge"
                except Exception as e:  # noqa: BLE001
                    unread = True
                    unverified.append({"entity_uid": row["entity_uid"], "live_slug": slug,
                                       "error": str(e)[:200]})
        pairs.append({"local": row, "live": live, "by": by, "unread": unread})
    return pairs, unverified


def _finding(kind: str, local: Optional[dict], live: Optional[dict], *,
             live_counted: bool = True, live_unread: bool = False,
             duplicate_of: Optional[str] = None) -> Optional[dict]:
    local_url = local["website"] if local else None
    live_url = live.get("website") if live else None
    on_local = is_listing_url(local_url)
    on_live = live_counted and is_listing_url(live_url)
    if not (on_local or on_live):
        return None
    return {
        "kind": kind,
        "name": local["name"] if local else live.get("name"),
        "side": "both" if on_local and on_live else "live" if on_live else "local",
        "url": live_url if on_live else local_url,
        "entity_uid": local["entity_uid"] if local else None,
        "live_slug": live.get("slug") if live else None,
        "live_id": live.get("id") if live else None,
        "local_website": local_url,
        "live_website": live_url,
        "live_unread": live_unread,
        "duplicate_of": duplicate_of,
    }
