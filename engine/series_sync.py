"""Series membership and Series-shared enrichment, local ↔ live.

- `mirror` / `reread` — copy the `series_slug` / `series_id` TownLoops
  reports for events this agent shipped onto their local rows. Only ever
  from a TownLoops response: a curator's (or the backfill's) later linking
  reaches local rows this way; nothing here infers membership.
- `fill_occurrence` — fill one live occurrence's blank Series-shared fields
  from its siblings (`engine.series.shared_fill`), as a full-body PATCH +
  GET verify (`engine.live_patch`). Live values win; only blanks fill.
- `sweep` — the daily pass (entity-live-sync): re-read membership, then
  fill the upcoming occurrences this agent shipped.

Never writes a Series.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from engine import series
from engine.derive_helpers import record_activity, utc_z
from engine.live_patch import apply_event_patch


_WORKER = "series-sync"
# How far back the daily re-read looks for events this agent shipped.
REREAD_DAYS_BACK = 60


def mirror(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Set the local Series columns of every local event shipped as one of
    these live `rows` to what the row says. Returns rows changed."""
    changed = 0
    for r in rows:
        slug = r.get("slug")
        if not slug or "series_slug" not in r:
            continue
        cur = conn.execute(
            "UPDATE events SET series_slug = ?, series_id = ? WHERE townloops_slug = ? "
            "AND (series_slug IS NOT ? OR series_id IS NOT ?)",
            (r.get("series_slug"), r.get("series_id"), slug,
             r.get("series_slug"), r.get("series_id")))
        changed += cur.rowcount
    return changed


def reread(client, conn: sqlite3.Connection, now: Optional[datetime] = None) -> int:
    """Re-read the live events this agent may have shipped (starting from
    REREAD_DAYS_BACK ago) and mirror their Series. Returns rows changed."""
    now = now or datetime.now(timezone.utc)
    rows = client.paginate("events", **{"from": utc_z(now - timedelta(days=REREAD_DAYS_BACK))})
    return mirror(conn, rows)


def siblings(client, series_slug: str) -> list[dict]:
    """Every live occurrence of a Series (the `?series=` filter is
    `is_active`-agnostic: an archived Series still groups)."""
    return list(client.paginate("events", series=series_slug))


def fill_occurrence(client, row: dict, sibs: Optional[list[dict]] = None) -> tuple[list[str], list[str]]:
    """Fill `row`'s blank Series-shared fields from its siblings. Returns
    (fields filled, mismatches); API errors raise. Nothing to do for a row
    with no Series."""
    series_slug = row.get("series_slug")
    if not series_slug:
        return [], []
    sibs = siblings(client, series_slug) if sibs is None else sibs
    overrides = series.shared_fill(row, sibs)
    if not overrides:
        return [], []
    result = apply_event_patch(client, row["slug"], overrides, current=row)
    return sorted(overrides), result.mismatches


def fill_and_record(client, conn: sqlite3.Connection, row: dict, worker: str, target: str,
                    sibs: Optional[list[dict]] = None) -> str:
    """`fill_occurrence` with the outcome in agent_activity: "filled",
    "unchanged" or "failed". A failure is recorded for the operator, never
    retried blindly — the occurrence is live either way."""
    try:
        filled, mismatches = fill_occurrence(client, row, sibs)
    except Exception as e:  # noqa: BLE001 — recorded; the occurrence is live regardless
        record_activity(conn, worker, "series-fill", target, ok=False,
                        error=f"{row.get('slug')}: {str(e)[:300]}")
        return "failed"
    if mismatches:
        record_activity(conn, worker, "series-fill", target, ok=False,
                        error=f"live event {row['slug']} doesn't match the PATCH: "
                              + "; ".join(mismatches)[:900])
        return "failed"
    if not filled:
        return "unchanged"
    record_activity(conn, worker, "series-fill", target,
                    detail=f"{row['slug']} ← Series {row['series_slug']}: {', '.join(filled)}")
    return "filled"


def sweep(client, conn: sqlite3.Connection, limit: int = 25,
          now: Optional[datetime] = None) -> dict[str, int]:
    """Daily: re-read Series membership, then fill blanks on up to `limit`
    upcoming occurrences this agent shipped that are in a Series."""
    now = now or datetime.now(timezone.utc)
    counts = {"reread": reread(client, conn, now), "filled": 0, "failed": 0}
    conn.commit()
    rows = conn.execute(
        "SELECT event_uid, townloops_slug, series_slug FROM events "
        "WHERE series_slug IS NOT NULL AND townloops_slug IS NOT NULL "
        "  AND pushed_at IS NOT NULL AND date_utc >= ? "
        "ORDER BY series_slug, date_utc LIMIT ?", (utc_z(now), limit)).fetchall()
    cache: dict[str, Optional[list[dict]]] = {}
    for uid, slug, series_slug in rows:
        if series_slug not in cache:
            try:
                cache[series_slug] = siblings(client, series_slug)
            except Exception as e:  # noqa: BLE001 — the Series' rows wait for tomorrow
                record_activity(conn, _WORKER, "series-fill", uid, ok=False,
                                error=f"list Series {series_slug}: {str(e)[:300]}")
                cache[series_slug] = None
                counts["failed"] += 1
        if cache[series_slug] is None:
            continue
        live = next((r for r in cache[series_slug] if r.get("slug") == slug), None)
        if live is None:   # moved out of the Series since the re-read
            continue
        status = fill_and_record(client, conn, live, _WORKER, uid, cache[series_slug])
        counts[status] = counts.get(status, 0) + 1
        conn.commit()
    return counts
