"""Bring local entity changes (e.g. enrichment) to an entity already on
TownLoops.

An entity we created or adopted live carries `townloops_slug` + `pushed_at`;
`pushed_hash` is the `content_hash` live was last verified to reflect. When
they differ (or pushed_hash was never set, as for an adopted row), the
entity is synced:

1. resync from live — every non-blank live value is copied into the local
   row (live wins: an operator's edit or a richer live value is kept);
2. fill live's blanks — local values for fields live has empty go out as a
   full-body PATCH (`engine/live_patch.apply_entity_patch`), never over a
   live value;
   a listing-page `website` (engine/listing_hosts) is copied neither way, so
   one the listing-website cleanup cleared on one side never comes back;
3. GET verify — only then does `pushed_hash` move.

A live row a curator merged into another (its 404 names the survivor,
`merged_into_slug`) is re-pointed: `townloops_slug` and `live_id` move to the
survivor, which is synced from step 1 — a 404 on PATCH re-reads it, so its
blanks are decided on its own values. A 404 naming no survivor is looked up
by the entity's Ingest Ref (engine/ingest_ref) before anything is flagged: a
row renamed on TownLoops is found under its new slug and re-pointed the same
way. A live row that is otherwise gone (404 on GET or PATCH and no row
carries the ref, or a merge chain past `live_lookup.MAX_MERGE_HOPS`) or
that doesn't show what was sent is flagged for the operator
(`extra_json.live_sync`, a failed `entity-sync` activity) and left alone
until cleared — never re-created. Never synced: live mirrors
(`source_kind='live'` — their local image is an operator proposal, not ours
to ship) and rows the server filed under another town
(`extra_json.cross_town` — our town-scoped GET/PATCH can't reach them, so a
404 there doesn't mean "gone").
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from engine.derive_helpers import iso_utc_now, parse_extra_json, record_activity
from engine.live_patch import ENTITY_FIELDS, apply_entity_patch, sendable


_WORKER = "entity-sync"
RESOURCES = {"venue": "venues", "business": "businesses", "community": "communities",
             "performer": "performers", "creator": "creators"}


@dataclass
class SyncResult:
    status: Literal["synced", "skipped", "missing", "mismatch", "error"]
    pulled: list[str] = field(default_factory=list)   # local fields taken from live
    pushed: list[str] = field(default_factory=list)   # live blanks filled from local
    detail: str = ""


def content_hash(kind: str, row: dict) -> str:
    """sha1 over the entity's TownLoops payload fields — what changes when
    enrichment fills a field, and what pushed_hash is compared against."""
    payload = {k: row.get(k) or "" for k in ENTITY_FIELDS[RESOURCES[kind]]}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def refresh_content_hash(conn: sqlite3.Connection, kind: str, entity_uid: str) -> None:
    table = RESOURCES[kind]
    r = conn.execute(f"SELECT * FROM {table} WHERE entity_uid = ?", (entity_uid,)).fetchone()
    if r:
        conn.execute(f"UPDATE {table} SET content_hash = ? WHERE entity_uid = ?",
                     (content_hash(kind, dict(r)), entity_uid))


def live_sync_flag(row: dict) -> dict | None:
    """The operator flag on an entity row ({status, at, detail}), if any."""
    return parse_extra_json(row.get("extra_json")).get("live_sync")


def is_cross_town(row: dict) -> bool:
    return bool(parse_extra_json(row.get("extra_json")).get("cross_town"))


def needs_sync(row: dict) -> bool:
    """Shipped by us (in our town), changed since live was last verified,
    not flagged."""
    extra = parse_extra_json(row.get("extra_json"))
    return bool(row.get("source_kind") != "live" and row.get("townloops_slug")
                and row.get("pushed_at") and not ({"live_sync", "cross_town"} & extra.keys())
                and row.get("pushed_hash") != row.get("content_hash"))


def _blank(value) -> bool:
    return not (isinstance(value, str) and value.strip())


def _listing_website(key: str, value) -> bool:
    """A directory/listing page as `website` (engine/listing_hosts): never
    copied either way — a cleared one would otherwise come back."""
    from engine.listing_hosts import is_listing_url
    return key == "website" and is_listing_url(value)


def sync_entity(client, conn: sqlite3.Connection, kind: str, entity_uid: str) -> SyncResult:
    """Sync one entity to its live row (see module doc). `skipped` when it
    doesn't need it; `error` (transient — retried next time) on API errors
    other than a 404."""
    return _sync(client, conn, kind, entity_uid, hops=0)


def _sync(client, conn: sqlite3.Connection, kind: str, entity_uid: str,
          hops: int) -> SyncResult:
    """sync_entity, `hops` merges into the PATCH-404 retries."""
    from engine import categories as cats
    from engine.live_lookup import MAX_MERGE_HOPS, get_live, is_not_found, merged_into_slug
    resource = RESOURCES[kind]
    r = conn.execute(f"SELECT * FROM {resource} WHERE entity_uid = ?", (entity_uid,)).fetchone()
    if not r or not needs_sync(dict(r)):
        return SyncResult("skipped")
    row = dict(r)
    slug = row["townloops_slug"]
    fields = ENTITY_FIELDS[resource]
    try:
        live = get_live(client, resource, slug)
    except Exception as e:  # noqa: BLE001
        if not is_not_found(e):
            return _error(conn, kind, row, f"read live {kind} {slug} failed: {str(e)[:200]}")
        try:
            live = _by_ref(client, conn, kind, entity_uid)
        except Exception as e2:  # noqa: BLE001
            return _error(conn, kind, row, f"look up live {kind} by ref failed: {str(e2)[:200]}")
        if not live:
            return flag_for_operator(conn, kind, row, "missing",
                                     f"live {kind} {slug} not found (404, no row carries "
                                     f"its ingest_ref)")
    if live.get("slug") and (live["slug"] != slug or hops
                             or (live.get("id") is not None
                                 and parse_extra_json(row.get("extra_json")).get("live_id")
                                 != live["id"])):
        # A survivor or a renamed row (on a hop, re-read for its id after a
        # PATCH 404), or a row bound without its id (a 409 binding).
        repoint(conn, kind, row, live)
        slug = row["townloops_slug"]

    pulled = [k for k in fields if not _blank(live.get(k)) and row.get(k) != live[k]
              and not _listing_website(k, live[k])]
    for k in pulled:
        row[k] = live[k]
    town_categories = cats.town_category_slugs()
    fill = {k: row[k] for k in fields
            if k != "name" and _blank(live.get(k)) and sendable(resource, k, row.get(k))
            and not _listing_website(k, row[k])
            and (k != "category_slug" or row[k] in town_categories)}

    if fill:
        try:
            result = apply_entity_patch(client, resource, slug, fill, current=live)
        except Exception as e:  # noqa: BLE001
            survivor = merged_into_slug(e)
            if not survivor and is_not_found(e) and hops < MAX_MERGE_HOPS:
                try:   # renamed since the read?
                    survivor = (_by_ref(client, conn, kind, entity_uid) or {}).get("slug")
                except Exception:  # noqa: BLE001 — flagged below like any 404
                    survivor = None
                survivor = survivor if survivor != slug else None
            if survivor and hops < MAX_MERGE_HOPS:
                # Merged or renamed since the read: start over on its own row.
                repoint(conn, kind, row, {"slug": survivor})
                return _sync(client, conn, kind, entity_uid, hops + 1)
            _save_pulled(conn, kind, row, pulled)
            if is_not_found(e):
                return flag_for_operator(conn, kind, row, "missing",
                                         f"live {kind} {slug} not found (404 on PATCH)", pulled)
            return _error(conn, kind, row, f"patch live {kind} {slug} failed: {str(e)[:200]}",
                          pulled)
        if result.mismatches:
            _save_pulled(conn, kind, row, pulled)
            return flag_for_operator(conn, kind, row, "mismatch",
                                     f"live {kind} {slug} doesn't match the PATCH: "
                                     + "; ".join(result.mismatches), pulled)

    _save_pulled(conn, kind, row, pulled)
    extra = parse_extra_json(row.get("extra_json"))
    if extra.pop("sync_error_at", None):
        conn.execute(f"UPDATE {resource} SET extra_json = ? WHERE entity_uid = ?",
                     (json.dumps(extra) if extra else None, entity_uid))
    conn.execute(f"UPDATE {resource} SET pushed_hash = content_hash, pushed_at = ? "
                 "WHERE entity_uid = ?", (iso_utc_now(), entity_uid))
    detail = f"{slug}: pulled {','.join(pulled) or '-'}; pushed {','.join(sorted(fill)) or '-'}"
    if pulled or fill:
        record_activity(conn, _WORKER, "synced", f"{kind}:{entity_uid}", detail=detail)
    return SyncResult("synced", pulled, sorted(fill), detail)


def _by_ref(client, conn: sqlite3.Connection, kind: str, entity_uid: str) -> dict | None:
    """The live row carrying the entity's Ingest Ref, if any — remembered
    locally when found. API errors raise."""
    from engine.ingest_ref import find_by_ref, ref_for, remember
    ref = ref_for(entity_uid)
    live = find_by_ref(client, RESOURCES[kind], ref)
    if live:
        remember(conn, kind, entity_uid, ref)
    return live


def repoint(conn: sqlite3.Connection, kind: str, row: dict, survivor: dict) -> None:
    """The entity's live row is now `survivor` (a live row, or just its
    {slug}) — a curator merged it there, or renamed it (found by its Ingest
    Ref): record it as the entity's live row from now on —
    `townloops_slug`, and `extra_json.live_id` when its id is known (event-
    ship binds venues by it). Local bookkeeping only; `row` is updated too."""
    old = row["townloops_slug"]
    extra = parse_extra_json(row.get("extra_json"))
    if survivor.get("id") is not None:
        extra["live_id"] = survivor["id"]
    else:
        extra.pop("live_id", None)
    row["townloops_slug"] = survivor["slug"]
    row["extra_json"] = json.dumps(extra) if extra else None
    conn.execute(f"UPDATE {RESOURCES[kind]} SET townloops_slug = ?, extra_json = ? "
                 "WHERE entity_uid = ?", (row["townloops_slug"], row["extra_json"],
                                          row["entity_uid"]))
    if old != survivor["slug"]:
        record_activity(conn, _WORKER, "repointed", f"{kind}:{row['entity_uid']}",
                        detail=f"{old} → {survivor['slug']} (merged or renamed live)")


def _save_pulled(conn: sqlite3.Connection, kind: str, row: dict, pulled: list[str]) -> None:
    resource = RESOURCES[kind]
    if pulled:
        sets = ", ".join(f"{k} = ?" for k in pulled)
        conn.execute(f"UPDATE {resource} SET {sets}, last_modified_at = ? WHERE entity_uid = ?",
                     (*(row[k] for k in pulled), iso_utc_now(), row["entity_uid"]))
    refresh_content_hash(conn, kind, row["entity_uid"])


def _error(conn: sqlite3.Connection, kind: str, row: dict, msg: str,
           pulled: list[str] | None = None) -> SyncResult:
    """Transient failure: retried by the next sweep (see note_error)."""
    note_error(conn, kind, row["entity_uid"])
    return SyncResult("error", pulled or [], detail=msg)


def note_error(conn: sqlite3.Connection, kind: str, entity_uid: str) -> None:
    """Stamp a transient failure (`extra_json.sync_error_at`): passes that
    order by `failed_last()` retry the row after those that haven't failed
    lately, so a stubborn row can't starve them. A successful sync clears
    it."""
    table = RESOURCES[kind]
    r = conn.execute(f"SELECT extra_json FROM {table} WHERE entity_uid = ?",
                     (entity_uid,)).fetchone()
    extra = parse_extra_json(r[0] if r else None)
    extra["sync_error_at"] = iso_utc_now()
    conn.execute(f"UPDATE {table} SET extra_json = ? WHERE entity_uid = ?",
                 (json.dumps(extra), entity_uid))


def failed_last(alias: str = "") -> str:
    """ORDER BY terms: rows without a recent `sync_error_at` first, oldest
    failure next, then by entity_uid. `alias` qualifies the columns."""
    p = f"{alias}." if alias else ""
    return (f"COALESCE(json_extract(CASE WHEN json_valid({p}extra_json) THEN {p}extra_json END, "
            f"'$.sync_error_at'), ''), {p}entity_uid")


def flag_for_operator(conn: sqlite3.Connection, kind: str, row: dict,
                      status: Literal["missing", "mismatch"], msg: str,
                      pulled: list[str] | None = None) -> SyncResult:
    """Hand the entity to the operator: no sync touches it until
    `extra_json.live_sync` is cleared. An entity already flagged with this
    status is left as is (no repeat activity)."""
    extra = parse_extra_json(row.get("extra_json"))
    if (extra.get("live_sync") or {}).get("status") == status:
        return SyncResult(status, pulled or [], detail=msg)
    extra["live_sync"] = {"status": status, "at": iso_utc_now(), "detail": msg[:1000]}
    conn.execute(f"UPDATE {RESOURCES[kind]} SET extra_json = ? WHERE entity_uid = ?",
                 (json.dumps(extra), row["entity_uid"]))
    record_activity(conn, _WORKER, f"live-{status}", f"{kind}:{row['entity_uid']}",
                    ok=False, error=msg[:1000])
    return SyncResult(status, pulled or [], detail=msg)


def sweep(client, conn: sqlite3.Connection, limit: int = 25,
          kinds: tuple[str, ...] = tuple(RESOURCES)) -> dict[str, int]:
    """Sync up to `limit` entities that need it (any kind, in `kinds`
    order) — catches rows enriched outside event-ship, e.g. by
    event-reconcile. Returns a count per SyncResult status."""
    client.verify_town()
    counts: dict[str, int] = {}
    for kind in kinds:
        if limit <= 0:
            break
        uids = [r[0] for r in conn.execute(
            f"SELECT entity_uid FROM {RESOURCES[kind]} "
            "WHERE COALESCE(source_kind, '') != 'live' AND townloops_slug IS NOT NULL "
            "  AND pushed_at IS NOT NULL "
            "  AND (pushed_hash IS NULL OR pushed_hash != content_hash) "
            "  AND NOT (json_valid(COALESCE(extra_json, '{}')) "
            "           AND (json_extract(COALESCE(extra_json, '{}'), '$.live_sync') IS NOT NULL "
            "                OR json_extract(COALESCE(extra_json, '{}'), '$.cross_town') IS NOT NULL)) "
            f"ORDER BY {failed_last()} LIMIT ?", (limit,))]
        for uid in uids:
            status = sync_entity(client, conn, kind, uid).status
            counts[status] = counts.get(status, 0) + 1
            conn.commit()
        limit -= len(uids)
    return counts


def clear_flag(conn: sqlite3.Connection, kind: str, entity_uid: str) -> bool:
    """Operator: the flagged entity was looked at (re-linked, or the live
    row restored) — let syncs touch it again. False if it wasn't flagged."""
    table = RESOURCES[kind]
    r = conn.execute(f"SELECT extra_json FROM {table} WHERE entity_uid = ?",
                     (entity_uid,)).fetchone()
    extra = parse_extra_json(r["extra_json"]) if r else {}
    if "live_sync" not in extra:
        return False
    del extra["live_sync"]
    conn.execute(f"UPDATE {table} SET extra_json = ? WHERE entity_uid = ?",
                 (json.dumps(extra) if extra else None, entity_uid))
    return True
