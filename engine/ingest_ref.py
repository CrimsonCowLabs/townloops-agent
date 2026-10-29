"""The Ingest Ref — the agent's own stable ID for a row it ships, sent on
every create so TownLoops can tell it whether that row already exists
(townloops ADR 0011, issues #28/#29; this repo's #63).

A ref is `hermes-<town>-<uid16>` — the string the agent used to propose as a
slug. It is unique per town per kind on TownLoops and settable once, never
re-pointed. The agent never sends a `slug` on a create: TownLoops derives the
public URL, the ref is the identity.

- A create carrying a ref TownLoops already holds is `409 ingest_ref_taken`,
  whose `details.slug` names the existing row (the survivor when it was
  merged away): bind to it, no second request (`taken_slug`).
- Lookups go by ref first (`find_by_ref`, one `?ingest_ref=` list read); the
  old slug / name + date matches stay only as the fallback for rows shipped
  before refs, and such a row is given its ref through the set-once PATCH
  (`adopt`) as soon as it is found.

A change or cancellation submission (autonomous mode: a pending event for the
TownLoops merge flow) is a separate row, so it carries its own ref
(`change_ref_for` / `cancel_ref_for`), never the event's.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


TAKEN = "ingest_ref_taken"


def _town() -> str:
    return os.environ.get("TOWNLOOPS_TOWN_SLUG", "unknown")


def ref_for(uid: str) -> str:
    """The ref of the event or entity with local uid `uid`."""
    return f"hermes-{_town()}-{uid[:16]}"


def change_ref_for(uid: str) -> str:
    """The ref of event `uid`'s pending change submission."""
    return f"hermes-{_town()}-change-{uid[:16]}"


def cancel_ref_for(uid: str) -> str:
    """The ref of event `uid`'s pending cancellation submission."""
    return f"hermes-{_town()}-cancel-{uid[:16]}"


def taken_slug(exc: Exception) -> Optional[str]:
    """The existing row's slug when `exc` is a `409 ingest_ref_taken`, else
    None (any other error, or a 409 that names no slug)."""
    if getattr(exc, "status", None) != 409 or getattr(exc, "code", None) != TAKEN:
        return None
    details = getattr(exc, "details", None)
    slug = details.get("slug") if isinstance(details, dict) else None
    return slug if isinstance(slug, str) and slug else None


def find_by_ref(client, resource: str, ref: Optional[str]) -> Optional[dict]:
    """The live row of `resource` carrying `ref`, else None. At most one row
    in a town carries a ref; the list read excludes deleted events, so None
    is not a promise a create will succeed (that answers 409, `taken_slug`).
    API errors raise — the caller must not create on an unanswered lookup."""
    if not ref:
        return None
    rows = client.list(resource, ingest_ref=ref, limit=2) or []
    return rows[0] if rows else None


# Local table and uid column per kind ("event" + engine/entity_ship's five).
TABLES = {
    "event": ("events", "event_uid"),
    "venue": ("venues", "entity_uid"),
    "business": ("businesses", "entity_uid"),
    "community": ("communities", "entity_uid"),
    "performer": ("performers", "entity_uid"),
    "creator": ("creators", "entity_uid"),
}


def remember(conn, kind: str, uid: str, ref: str) -> None:
    """Record that the live row of local `kind` `uid` carries `ref`
    (the local `ingest_ref` column, migration 029). No commit."""
    table, col = TABLES[kind]
    conn.execute(f"UPDATE {table} SET ingest_ref = ? WHERE {col} = ?", (ref, uid))


@dataclass
class Adoption:
    status: str            # adopted | already | other-ref | taken | error
    detail: str = ""


def adopt(client, resource: str, live: dict, ref: str) -> Adoption:
    """Give a live row we shipped before refs existed our ref, through the
    set-once PATCH. A row already carrying it is `already`; one carrying a
    different ref is `other-ref` (never re-pointed — TownLoops refuses with a
    422); a ref another row holds is `taken` (a duplicate, for the operator)
    — unless the 409 names this very row: a row of ours merged into it holds
    the ref, which already resolves here (`already`). Never raises."""
    current = live.get("ingest_ref")
    if current == ref:
        return Adoption("already")
    if current:
        return Adoption("other-ref", f"{live.get('slug')} carries {current}")
    try:
        client.update(resource, live["slug"], {"ingest_ref": ref})
    except Exception as e:  # noqa: BLE001 — reported, retried by the next sweep
        other = taken_slug(e)
        if other and other == live.get("slug"):
            return Adoption("already", f"{ref} resolves to {other} (merged)")
        if other:
            return Adoption("taken", f"{ref} already identifies {other}")
        if getattr(e, "status", None) == 422:
            return Adoption("other-ref", str(e)[:200])
        return Adoption("error", str(e)[:200])
    return Adoption("adopted")
