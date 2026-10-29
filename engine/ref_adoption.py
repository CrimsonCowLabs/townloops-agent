"""Give rows the agent shipped before Ingest Refs existed their ref (#63,
townloops ADR 0011), through the set-once PATCH (`engine/ingest_ref.adopt`),
so recovery has one regime — find by ref — instead of two forever.

A row is ours to adopt when it is bound live by us: `townloops_slug` +
`pushed_at`, `ingest_ref` NULL (shipped before refs, not adopted yet), and
- not a live mirror (`source_kind='live'` — a local stand-in for a row
  someone else created; it never gets our ref);
- not filed under another town (`entity_sync.is_cross_town` — a town-scoped
  GET can't reach it);
- not flagged for the operator (`extra_json.live_sync` — no write touches it
  until cleared, as entity_sync);
- events: not a `duplicate` copy (it rides a sibling's live row, which is
  that sibling's to adopt) or `superseded` (no longer the live row's local
  row).
Rows the agent adopted (bound to a live row found by lookup) count as ours:
the ref is the agent's identity for its local row, whoever created the live
one — the TownLoops side keeps a set ref, so a row carrying another agent's
ref is `other-ref` and left alone.

Each row: GET its live row (`live_lookup.get_live`, following merges — a
survivor re-points the local row: entities via `entity_sync.repoint`, events
their `townloops_slug`), then `ingest_ref.adopt`:
- adopted / already → `ingest_ref.remember` (the local column);
- other-ref → nothing remembered; marked `extra_json.ref_adoption` and one
  `other-ref` activity, never tried again;
- taken → another live row holds our ref: a duplicate for the operator.
  Marked, one failed `ref-taken` activity, never tried again;
- a 404 naming no survivor → `unbound`, stamped to go behind the rest and
  retried; event-ingest's name + date `resync` is that row's fallback, so it
  is not flagged here;
- any other error → stamped, retried next run.
Never-tried rows go first, then the oldest-shipped; stamped rows after.
Fill-blank PATCH, so autonomous mode allows it: the plain client.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter

from engine import entity_sync, ingest_ref
from engine.derive_helpers import iso_utc_now, parse_extra_json, record_activity
from engine.live_lookup import get_live, is_not_found


_WORKER = "ref-adoption"
MARK = "ref_adoption"
FINAL = ("other-ref", "taken")         # marked statuses never retried
RESOURCES = {"event": "events", **entity_sync.RESOURCES}


def _json_path(path: str) -> str:
    return (f"json_extract(CASE WHEN json_valid(extra_json) THEN extra_json END, '{path}')")


def _candidates(conn: sqlite3.Connection, kind: str, limit: int) -> list[dict]:
    table, uid_col = ingest_ref.TABLES[kind]
    extra = ("AND COALESCE(status, '') NOT IN ('duplicate', 'superseded') "
             if kind == "event" else "")
    tried = f"COALESCE({_json_path('$.' + MARK + '.at')}, '')"
    return [dict(r) for r in conn.execute(
        f"SELECT *, {uid_col} AS uid, {tried} AS tried_at FROM {table} "
        "WHERE townloops_slug IS NOT NULL AND pushed_at IS NOT NULL AND ingest_ref IS NULL "
        "  AND COALESCE(source_kind, '') != 'live' "
        f"  AND {_json_path('$.cross_town')} IS NULL "
        f"  AND {_json_path('$.live_sync')} IS NULL "
        f"  AND COALESCE({_json_path('$.' + MARK + '.status')}, '') NOT IN ('other-ref', 'taken') "
        f"  {extra}"
        f"ORDER BY tried_at, pushed_at, {uid_col} LIMIT ?", (limit,))]


def _mark(conn: sqlite3.Connection, kind: str, uid: str, status: str, detail: str = "") -> None:
    table, col = ingest_ref.TABLES[kind]
    r = conn.execute(f"SELECT extra_json FROM {table} WHERE {col} = ?", (uid,)).fetchone()
    extra = parse_extra_json(r[0] if r else None)
    extra[MARK] = {"status": status, "at": iso_utc_now(), "detail": detail[:500]}
    conn.execute(f"UPDATE {table} SET extra_json = ? WHERE {col} = ?", (json.dumps(extra), uid))


def _clear_mark(conn: sqlite3.Connection, kind: str, row: dict) -> None:
    extra = parse_extra_json(row.get("extra_json"))
    if extra.pop(MARK, None) is not None:
        table, col = ingest_ref.TABLES[kind]
        conn.execute(f"UPDATE {table} SET extra_json = ? WHERE {col} = ?",
                     (json.dumps(extra) if extra else None, row["uid"]))


def _repoint(conn: sqlite3.Connection, kind: str, row: dict, live: dict) -> None:
    if kind != "event":
        entity_sync.repoint(conn, kind, row, live)
        return
    old = row["townloops_slug"]
    conn.execute("UPDATE events SET townloops_slug = ? WHERE event_uid = ?", (live["slug"], row["uid"]))
    row["townloops_slug"] = live["slug"]
    record_activity(conn, _WORKER, "repointed", f"event:{row['uid']}",
                    detail=f"{old} now live as {live['slug']}")


def adopt_row(client, conn: sqlite3.Connection, kind: str, row: dict) -> str:
    """Adopt one candidate row (see module doc). Returns the outcome:
    adopted | already | other-ref | taken | unbound | error. No commit."""
    resource, uid = RESOURCES[kind], row["uid"]
    target = f"{kind}:{uid}"
    ref = ingest_ref.ref_for(uid)
    try:
        live = get_live(client, resource, row["townloops_slug"])
    except Exception as e:  # noqa: BLE001 — stamped, retried next run
        status = "unbound" if is_not_found(e) else "error"
        _mark(conn, kind, uid, status, str(e)[:200])
        return status
    if live.get("slug") and live["slug"] != row["townloops_slug"]:
        _repoint(conn, kind, row, live)
    result = ingest_ref.adopt(client, resource, live, ref)
    if result.status in ("adopted", "already"):
        ingest_ref.remember(conn, kind, uid, ref)
        _clear_mark(conn, kind, row)
        if result.status == "adopted":
            record_activity(conn, _WORKER, "adopted", target,
                            detail=f"{row['townloops_slug']} ← {ref}")
    elif result.status == "other-ref":
        _mark(conn, kind, uid, result.status, result.detail)
        record_activity(conn, _WORKER, "other-ref", target,
                        detail=f"{row['townloops_slug']}: {result.detail}")
    elif result.status == "taken":
        _mark(conn, kind, uid, result.status, result.detail)
        record_activity(conn, _WORKER, "ref-taken", target, ok=False,
                        error=f"live {kind} {row['townloops_slug']}: {result.detail} "
                              "— a duplicate on TownLoops")
    else:
        _mark(conn, kind, uid, "error", result.detail)
    return result.status


def adopt_shipped(client, conn: sqlite3.Connection, limit: int = 25,
                  kinds: tuple[str, ...] = tuple(RESOURCES)) -> dict[str, int]:
    """Adopt refs for up to `limit` pre-ref rows across `kinds`, never-tried
    and oldest-shipped first. Commits per row. Returns a count per outcome
    (empty when there was nothing to adopt)."""
    if limit <= 0:
        return {}
    pool = [(kind, r) for kind in kinds for r in _candidates(conn, kind, limit)]
    if not pool:
        return {}
    client.verify_town()
    pool.sort(key=lambda kr: (kr[1]["tried_at"], kr[1]["pushed_at"] or "", kr[1]["uid"]))
    counts: Counter[str] = Counter()
    for kind, row in pool[:limit]:
        counts[adopt_row(client, conn, kind, row)] += 1
        conn.commit()
    record_activity(conn, _WORKER, "sweep", "ingest-ref",
                    detail=json.dumps(dict(counts), sort_keys=True))
    conn.commit()
    return dict(counts)
