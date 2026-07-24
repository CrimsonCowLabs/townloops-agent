"""Shared enrichment loop used by venue/business/performer skills.

Contract per kind:
  table_name     — SQLite table holding the entity rows
  api_resource   — TownLoops API resource path segment (plural)
  fillable       — payload keys the enrichment step may fill (never overwrite)
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from engine import db as _db
from engine import lock as _lock
from engine import research as _research
from engine import townloops_api


@dataclass(frozen=True)
class KindSpec:
    kind: str
    table: str
    resource: str
    fillable: tuple[str, ...]


# Fillable = payload keys enrichment may populate on rows where the field is
# currently NULL. Names match the OpenAPI payload contract exactly (bare
# `youtube` for business/community/performer; `youtube_url` on creator).
_SOCIAL_FIELDS = ("instagram", "facebook", "tiktok", "twitter", "youtube", "linkedin")

SPECS: dict[str, KindSpec] = {
    "venue": KindSpec(
        "venue",
        "venues",
        "venues",
        ("description", "address", "website", "phone", "image_path", "image_alt"),
    ),
    "business": KindSpec(
        "business",
        "businesses",
        "businesses",
        ("description", "address", "website", "phone", "image_path", "image_alt",
         *_SOCIAL_FIELDS),
    ),
    "performer": KindSpec(
        "performer",
        "performers",
        "performers",
        ("description", "website", "instagram", "spotify_url", "image_path", "image_alt",
         "facebook", "tiktok", "twitter", "youtube", "linkedin"),
    ),
    "community": KindSpec(
        "community",
        "communities",
        "communities",
        ("description", "website", "meeting_info", "image_path", "image_alt",
         *_SOCIAL_FIELDS),
    ),
    "creator": KindSpec(
        "creator",
        "creators",
        "creators",
        ("bio", "website", "instagram", "youtube_url", "image_path", "image_alt",
         "facebook", "tiktok", "twitter", "linkedin"),
    ),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _worker_tag() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def claim(kind: str, limit: int, conn: sqlite3.Connection | None = None) -> list[int]:
    """Claim up to `limit` queue rows for this worker. Returns queue IDs."""
    spec = SPECS[kind]
    conn = conn or _db.connect()
    with conn:
        ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM enrichment_queue "
                "WHERE kind = ? AND claimed_at IS NULL AND resolved_at IS NULL "
                "ORDER BY queued_at LIMIT ?",
                (spec.kind, limit),
            ).fetchall()
        ]
        for qid in ids:
            conn.execute(
                "UPDATE enrichment_queue SET claimed_by = ?, claimed_at = ?, attempts = attempts + 1 "
                "WHERE id = ?",
                (_worker_tag(), _now(), qid),
            )
    return ids


def apply(kind: str, queue_ids: list[int], *, use_firecrawl: bool = False,
          conn: sqlite3.Connection | None = None) -> list[dict]:
    """Run the research pass on each claimed row; write filled fields back to the table.

    Returns per-row summaries.
    """
    spec = SPECS[kind]
    conn = conn or _db.connect()
    results: list[dict] = []
    for qid in queue_ids:
        q = conn.execute(
            "SELECT entity_uid FROM enrichment_queue WHERE id = ?", (qid,)
        ).fetchone()
        if not q:
            continue
        row = conn.execute(
            f"SELECT * FROM {spec.table} WHERE entity_uid = ?", (q["entity_uid"],)
        ).fetchone()
        if not row:
            _release(conn, qid, error="row missing")
            continue
        row = dict(row)
        research_url = row.get("website") or row.get("source_url")
        if not research_url:
            _release(conn, qid, error="no url to research")
            results.append({"entity_uid": row["entity_uid"], "skipped": True})
            continue

        result = _research.research_url(research_url, use_firecrawl=use_firecrawl)
        # YouTube is `youtube` on business/community/performer and `youtube_url`
        # on creator (per the OpenAPI payload contracts). Mirror both spellings
        # so the per-kind fillable tuple below picks up whichever it expects.
        if "youtube" in result.payload:
            result.payload.setdefault("youtube_url", result.payload["youtube"])
        if "youtube_url" in result.payload:
            result.payload.setdefault("youtube", result.payload["youtube_url"])
        merged = {}
        for key in spec.fillable:
            if not row.get(key) and result.payload.get(key):
                merged[key] = result.payload[key]
        if merged:
            sets = ", ".join(f"{k} = ?" for k in merged)
            values = list(merged.values()) + [_now(), row["entity_uid"]]
            conn.execute(
                f"UPDATE {spec.table} SET {sets}, "
                f"enrichment_status = 'ready', enriched_at = ? "
                f"WHERE entity_uid = ?",
                values,
            )
        else:
            conn.execute(
                f"UPDATE {spec.table} SET enrichment_status = 'done', enriched_at = ? "
                f"WHERE entity_uid = ?",
                (_now(), row["entity_uid"]),
            )
        _release(conn, qid)
        conn.commit()
        results.append(
            {
                "entity_uid": row["entity_uid"],
                "name": row.get("name"),
                "filled": list(merged.keys()),
                "notes": result.notes,
            }
        )
    return results


def _release(conn: sqlite3.Connection, qid: int, error: str | None = None) -> None:
    conn.execute(
        "UPDATE enrichment_queue SET resolved_at = ?, last_error = ? WHERE id = ?",
        (_now(), error, qid),
    )


def ship(kind: str, *, batch: int = 100, conn: sqlite3.Connection | None = None) -> dict:
    """PATCH ready rows back to TownLoops."""
    spec = SPECS[kind]
    tl = townloops_api.Client()
    conn = conn or _db.connect()
    rows = conn.execute(
        f"SELECT * FROM {spec.table} WHERE enrichment_status = 'ready' "
        f"AND townloops_slug IS NOT NULL LIMIT ?",
        (batch,),
    ).fetchall()
    shipped: list[str] = []
    errors: list[dict] = []
    for row in rows:
        row = dict(row)
        payload = {k: row.get(k) for k in spec.fillable if row.get(k)}
        if not payload:
            continue
        try:
            tl.update(spec.resource, row["townloops_slug"], payload)
            conn.execute(
                f"UPDATE {spec.table} SET enrichment_status = 'done', "
                f"pushed_at = ?, pushed_hash = ? WHERE entity_uid = ?",
                (_now(), row.get("content_hash"), row["entity_uid"]),
            )
            shipped.append(row["townloops_slug"])
        except townloops_api.TownloopsError as e:
            errors.append({"slug": row["townloops_slug"], "code": e.code, "msg": e.message})
    conn.commit()
    return {"kind": kind, "shipped": len(shipped), "errors": errors}


def resync_from_remote(kind: str, *, limit: int | None = None,
                        conn: sqlite3.Connection | None = None) -> dict:
    """Pull live TownLoops state for rows we've already shipped and copy remote
    field values into the local row.

    Fixes the enrichment overwrite risk: without this, `apply()` only writes
    fields that are locally NULL, so if the operator edits a field directly
    on TownLoops (via admin UI), the local row still looks blank and the next
    sweep re-fills it (potentially with worse data) and PATCHes it back over
    the operator edit. With resync, "local field is NULL" means "the field is
    genuinely blank on TownLoops" — enrichment's fill-blanks-only semantics
    are safe again.

    Runs read-only against TownLoops (GET only), writes only to the local DB.
    Only touches rows with a non-NULL townloops_slug (already shipped).
    """
    spec = SPECS[kind]
    conn = conn or _db.connect()
    tl = townloops_api.Client()

    # Discover the local table's column set so we can copy any remote key
    # that has a matching local column, without hardcoding a list.
    local_cols = {r["name"] for r in conn.execute(
        f"PRAGMA table_info({spec.table})"
    ).fetchall()}
    # Don't overwrite bookkeeping fields — only entity payload data.
    protected = {"entity_uid", "content_hash", "pushed_at", "pushed_hash",
                 "first_seen_at", "source", "source_kind", "source_url",
                 "enrichment_status", "enriched_at", "extra_json",
                 "last_seen_at", "last_modified_at", "townloops_slug"}
    copyable = local_cols - protected

    sql = f"SELECT entity_uid, townloops_slug FROM {spec.table} " \
          f"WHERE townloops_slug IS NOT NULL ORDER BY entity_uid"
    params: list = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    updated = 0
    remote_missing = 0
    errors: list[dict] = []
    for row in rows:
        slug = row["townloops_slug"]
        try:
            remote = tl.get(spec.resource, slug)
        except townloops_api.TownloopsError as exc:
            if exc.status == 404:
                remote_missing += 1
                continue
            errors.append({"entity_uid": row["entity_uid"], "slug": slug,
                           "code": exc.code, "msg": exc.message})
            continue
        if not isinstance(remote, dict):
            errors.append({"entity_uid": row["entity_uid"], "slug": slug,
                           "note": "remote shape not a dict"})
            continue
        # Build the update set: any copyable column whose remote value is
        # non-None. We do NOT skip cols where local matches remote — that's
        # a no-op UPDATE, cheap enough not to bother filtering.
        updates = {k: remote[k] for k in copyable
                   if k in remote and remote[k] is not None and remote[k] != ""}
        if not updates:
            continue
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        with conn:
            conn.execute(
                f"UPDATE {spec.table} SET {set_clause}, "
                f"last_modified_at = ? WHERE entity_uid = ?",
                list(updates.values()) + [_now(), row["entity_uid"]],
            )
            # After resync, the local row now matches remote — recompute
            # content_hash by treating the current row values as the source.
            # Ship logic uses content_hash != pushed_hash to decide PATCH;
            # aligning them here prevents a spurious PATCH round-trip.
            conn.execute(
                f"UPDATE {spec.table} SET pushed_hash = content_hash "
                f"WHERE entity_uid = ? AND pushed_at IS NOT NULL",
                (row["entity_uid"],),
            )
        updated += 1
    return {
        "kind": kind,
        "scanned": len(rows),
        "updated": updated,
        "remote_missing_404": remote_missing,
        "errors": errors,
    }


def enqueue(kind: str, entity_uid: str, conn: sqlite3.Connection | None = None) -> bool:
    """Idempotently push an entity onto the queue."""
    conn = conn or _db.connect()
    try:
        conn.execute(
            "INSERT INTO enrichment_queue(kind, entity_uid, queued_at) VALUES (?, ?, ?)",
            (kind, entity_uid, _now()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def sweep(kind: str, limit: int, *, use_firecrawl: bool = False,
          conn: sqlite3.Connection | None = None) -> dict:
    """Claim + apply in one call, guarded by an exclusive lock scoped to `kind`.

    If a prior sweep of the same kind is still running, returns a `skipped`
    result instead of piling on. This is the entry point cron should call.

    Always includes the per-run Firecrawl meter (`firecrawl`) in the summary
    so operators see when the invoice is climbing without correlating logs.
    """
    # Cross-town guard: fail-fast if $TOWNLOOPS_TOWN_SLUG doesn't match this
    # deployment's categories snapshot (prevents writing to the wrong town's
    # tables under a misconfigured env). Cached per-process, so cheap after
    # the first call.
    townloops_api.Client().verify_town()
    lock_name = f"enrichment-{kind}"
    stats_before = _research.firecrawl_stats()
    try:
        with _lock.acquire(lock_name):
            ids = claim(kind, limit, conn=conn)
            if not ids:
                return {"kind": kind, "claimed": 0, "results": [],
                        "queue_depth": queue_depth(kind, conn=conn),
                        "firecrawl": _diff_stats(stats_before, _research.firecrawl_stats())}
            results = apply(kind, ids, use_firecrawl=use_firecrawl, conn=conn)
            return {"kind": kind, "claimed": len(ids), "results": results,
                    "firecrawl": _diff_stats(stats_before, _research.firecrawl_stats())}
    except _lock.LockHeld as e:
        return {"kind": kind, "skipped": True, "reason": str(e),
                "queue_depth": queue_depth(kind, conn=conn)}


def _diff_stats(before: dict, after: dict) -> dict:
    return {
        "calls": after["calls"] - before["calls"],
        "ok": after["ok"] - before["ok"],
        "http_error": after["http_error"] - before["http_error"],
        "network_error": after["network_error"] - before["network_error"],
        "last_error": after["last_error"],
    }


def queue_depth(kind: str, conn: sqlite3.Connection | None = None) -> int:
    conn = conn or _db.connect()
    row = conn.execute(
        "SELECT COUNT(*) c FROM enrichment_queue "
        "WHERE kind = ? AND claimed_at IS NULL AND resolved_at IS NULL",
        (kind,),
    ).fetchone()
    return row["c"] if row else 0
