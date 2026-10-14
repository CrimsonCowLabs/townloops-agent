"""Read-only audit of venues, businesses and communities whose `website` is
a directory/listing page (`engine/listing_hosts.is_listing_url` — the one
check; seen on SLT: a business whose website was its restaurantji page).

Each local row is paired with its live row the way entity-sync knows it:
`townloops_slug` in the live listing, else `extra_json.live_id`, else a GET
of the slug that follows a curator's merge to the survivor
(`live_lookup.find_live_entity`). Rows filed under another town
(`extra_json.cross_town`) aren't paired — our town-scoped reads can't see
them. A live row no local row pairs with stands alone.

One finding per entity, its `side` saying which side holds a listing URL:
`local`, `live` or `both`. Both websites are always given as stored
(`None` where that side has no row), and `url` is the listing one — live's
when both are listings, even if they differ. The other side's own site, if
any, is in its `*_website` field.

Only LIST/GET calls and SELECTs: nothing is re-pointed or cleared here.
The cleanup (#12) works from these rows.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from engine.derive_helpers import dict_rows as _rows, parse_extra_json
from engine.listing_hosts import is_listing_url


# kind → TownLoops resource / local table, in report order.
KINDS = {"venue": "venues", "business": "businesses", "community": "communities"}


def audit_listing_websites(conn: sqlite3.Connection, client, *, limit: int = 200) -> dict:
    """find_listing_websites, summarised: uncapped counts per side and kind,
    and the first `limit` rows."""
    rows, unverified = _find(conn, client)
    counts = {"local": 0, "live": 0, "both": 0}
    by_kind = dict.fromkeys(KINDS, 0)
    for r in rows:
        counts[r["side"]] += 1
        by_kind[r["kind"]] += 1
    return {"total": len(rows), "counts": counts, "by_kind": by_kind,
            "rows": rows[:limit], "unverified": unverified}


def find_listing_websites(conn: sqlite3.Connection, client) -> list[dict]:
    """Every venue/business/community with a listing-page website, locally,
    live or both, ordered by kind then name. Each row:

        {"kind", "name", "side": "local"|"live"|"both", "url",
         "entity_uid" (None: no local row), "live_slug", "live_id" (None: not
         paired live), "local_website", "live_website"}
    """
    return _find(conn, client)[0]


def _find(conn: sqlite3.Connection, client) -> tuple[list[dict], list[dict]]:
    """(rows, unverified) — unverified: local rows whose live slug couldn't
    be read (an API error other than a 404), reported on the local side."""
    from engine.live_lookup import find_live_entity
    out, unverified = [], []
    for kind, resource in KINDS.items():
        live_rows = list(client.paginate(resource))
        by_slug = {r.get("slug"): r for r in live_rows}
        by_id = {r.get("id"): r for r in live_rows if r.get("id") is not None}
        paired: set[str] = set()
        for row in _rows(conn, f"SELECT entity_uid, name, website, townloops_slug, extra_json "
                               f"FROM {resource}"):
            extra = parse_extra_json(row["extra_json"])
            live = None
            if not extra.get("cross_town"):
                live = ((row["townloops_slug"] and by_slug.get(row["townloops_slug"]))
                        or by_id.get(extra.get("live_id")))
                if live is None and row["townloops_slug"]:
                    try:
                        live = find_live_entity(client, resource, row["townloops_slug"])
                    except Exception as e:  # noqa: BLE001
                        unverified.append({"kind": kind, "entity_uid": row["entity_uid"],
                                           "live_slug": row["townloops_slug"],
                                           "error": str(e)[:200]})
            if live is not None:
                paired.add(live.get("slug"))
            finding = _finding(kind, row, live)
            if finding:
                out.append(finding)
        for live in live_rows:
            if live.get("slug") not in paired:
                finding = _finding(kind, None, live)
                if finding:
                    out.append(finding)
    order = list(KINDS)
    out.sort(key=lambda r: (order.index(r["kind"]), (r["name"] or "").lower(),
                            r["live_slug"] or "", r["entity_uid"] or ""))
    return out, unverified


def _finding(kind: str, local: Optional[dict], live: Optional[dict]) -> Optional[dict]:
    local_url = local["website"] if local else None
    live_url = live.get("website") if live else None
    on_local, on_live = is_listing_url(local_url), is_listing_url(live_url)
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
    }
