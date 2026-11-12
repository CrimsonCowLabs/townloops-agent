"""`discovery_checks` (migration 032): what the daily discovery runs have
already looked at — seed-hunt queries, mined sites and domains, unreachable
calendar URLs (engine/seed_hunt), organisation-harvest terms
(engine/harvest_terms) — so the next run moves on instead of repeating
itself. One row per key, the latest look wins."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from engine.derive_helpers import iso_utc_now


def checked_at(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT checked_at FROM discovery_checks WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def recently_checked(conn: sqlite3.Connection, key: str, days: int,
                     now: Optional[datetime] = None) -> bool:
    at = checked_at(conn, key)
    if not at:
        return False
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    return at >= since.isoformat(timespec="seconds")


def mark(conn: sqlite3.Connection, key: str, outcome: str, detail: Optional[dict] = None) -> None:
    """Record a look at `key` now (commits)."""
    conn.execute("INSERT INTO discovery_checks (key, checked_at, outcome, detail) "
                 "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "checked_at = excluded.checked_at, outcome = excluded.outcome, "
                 "detail = excluded.detail",
                 (key, iso_utc_now(), outcome,
                  json.dumps(detail, ensure_ascii=False) if detail else None))
    conn.commit()
