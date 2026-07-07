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
from engine import research as _research
from engine import townloops_api


@dataclass(frozen=True)
class KindSpec:
    kind: str
    table: str
    resource: str
    fillable: tuple[str, ...]


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
        ("description", "address", "website", "phone", "image_path", "image_alt"),
    ),
    "performer": KindSpec(
        "performer",
        "performers",
        "performers",
        ("description", "website", "instagram", "spotify_url", "image_path", "image_alt"),
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


def queue_depth(kind: str, conn: sqlite3.Connection | None = None) -> int:
    conn = conn or _db.connect()
    row = conn.execute(
        "SELECT COUNT(*) c FROM enrichment_queue "
        "WHERE kind = ? AND claimed_at IS NULL AND resolved_at IS NULL",
        (kind,),
    ).fetchone()
    return row["c"] if row else 0
