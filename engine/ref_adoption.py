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
their `townloops_slug`), then `ingest_ref.adopt_and_remember` — the one
sequence event-ship, entity_ship and event-ingest's resync use too:
- adopted / already → `ingest_ref.remember` (the local column);
- other-ref → nothing remembered; stamped `extra_json.ref_adoption` (FINAL)
  and one `adopt-ref` activity, never tried again — here or by a ship;
- taken → another live row holds our ref: a duplicate for the operator.
  Stamped (FINAL), one failed `adopt-ref` activity, never tried again;
- a 404 naming no survivor → `unbound`, stamped to go behind the rest and
  retried; event-ingest's name + date `resync` is that row's fallback, so it
  is not flagged here;
- any other error → stamped (a failed `adopt-ref`), retried next run.
Never-tried rows go first, then the oldest-shipped; stamped rows after.
Fill-blank PATCH, so autonomous mode allows it: the plain client.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter

from engine import entity_sync, ingest_ref
from engine.derive_helpers import parse_extra_json, record_activity
from engine.live_lookup import get_live, is_not_found


_WORKER = "ref-adoption"
RESOURCES = ingest_ref.RESOURCES


def _json_path(path: str) -> str:
    return (f"json_extract(CASE WHEN json_valid(extra_json) THEN extra_json END, '{path}')")


def _candidates(conn: sqlite3.Connection, kind: str, limit: int) -> list[dict]:
    table, uid_col = RESOURCES[kind], ingest_ref.uid_column(kind)
    extra = ("AND COALESCE(status, '') NOT IN ('duplicate', 'superseded') "
             if kind == "event" else "")
    mark = "$." + ingest_ref.MARK
    final = ", ".join(f"'{s}'" for s in ingest_ref.FINAL)
    tried = f"COALESCE({_json_path(mark + '.at')}, '')"
    return [dict(r) for r in conn.execute(
        f"SELECT *, {uid_col} AS uid, {tried} AS tried_at FROM {table} "
        "WHERE townloops_slug IS NOT NULL AND pushed_at IS NOT NULL AND ingest_ref IS NULL "
        "  AND COALESCE(source_kind, '') != 'live' "
        f"  AND {_json_path('$.cross_town')} IS NULL "
        f"  AND {_json_path('$.live_sync')} IS NULL "
        f"  AND COALESCE({_json_path(mark + '.status')}, '') NOT IN ({final}) "
        f"  {extra}"
        f"ORDER BY tried_at, pushed_at, {uid_col} LIMIT ?", (limit,))]


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
    try:
        live = get_live(client, resource, row["townloops_slug"])
    except Exception as e:  # noqa: BLE001 — stamped, retried next run
        status = "unbound" if is_not_found(e) else "error"
        ingest_ref.mark(conn, kind, uid, status, str(e)[:200])
        return status
    if live.get("slug") and live["slug"] != row["townloops_slug"]:
        _repoint(conn, kind, row, live)
    return ingest_ref.adopt_and_remember(client, conn, kind, uid, live, worker=_WORKER).status


def adopt_shipped(client, conn: sqlite3.Connection, limit: int = 25,
                  kinds: tuple[str, ...] = tuple(RESOURCES)) -> dict[str, int]:
    """Adopt refs for up to `limit` pre-ref rows across `kinds`, never-tried
    and oldest-shipped first; a kind the API takes no refs for
    (`ingest_ref.supported`, #64) is skipped. Commits per row. Returns a
    count per outcome (empty when there was nothing to adopt)."""
    if limit <= 0:
        return {}
    pool = [(kind, r) for kind in kinds if ingest_ref.supported(kind)
            for r in _candidates(conn, kind, limit)]
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


# --------------------------------------------------------------------------
# Bindings an ignored ref filter may have made (#64) — read-only
# --------------------------------------------------------------------------

def audit_bindings(client, conn: sqlite3.Connection) -> list[dict]:
    """Local entities whose live binding a ref lookup may have made against a
    server ignoring `?ingest_ref=` (SLT bound unrelated venues to
    `aleworx-stateline` that way, then synced onto it). Suspect when:
    - `ref-unsupported` — a ref remembered for a kind the API takes no refs
      for (`ingest_ref.supported`): only such a lookup or adoption set it;
    - `ref-not-carried` — the live row doesn't carry the remembered ref;
    - `shared-slug` — several local rows of the kind bound to one live row.
    Each suspect: its live row's name and ref (one GET, no merge-following,
    no re-point; `live_unread` on failure), the local rows sharing it, and
    the `entity-sync` activities that pushed fields to it (`patched`). Live
    mirrors are skipped. GET only — writes nothing."""
    client.verify_town()
    out = []
    for kind, table in entity_sync.RESOURCES.items():
        rows = [dict(r) for r in conn.execute(
            f"SELECT entity_uid, name, townloops_slug, ingest_ref, extra_json FROM {table} "
            "WHERE townloops_slug IS NOT NULL AND COALESCE(source_kind, '') != 'live' "
            "ORDER BY townloops_slug, entity_uid")]
        by_slug: dict[str, list[str]] = {}
        for r in rows:
            by_slug.setdefault(r["townloops_slug"], []).append(r["entity_uid"])
        supported = ingest_ref.supported(kind)
        for r in rows:
            shared = [u for u in by_slug[r["townloops_slug"]] if u != r["entity_uid"]]
            if not (r["ingest_ref"] or shared):
                continue
            reasons = ["shared-slug"] if shared else []
            if r["ingest_ref"] and not supported:
                reasons.append("ref-unsupported")
            entry = {"kind": kind, "entity_uid": r["entity_uid"], "name": r["name"],
                     "slug": r["townloops_slug"], "ingest_ref": r["ingest_ref"],
                     "live_id": parse_extra_json(r["extra_json"]).get("live_id"),
                     "shares_with": shared}
            try:
                live = client.get(table, r["townloops_slug"]) or {}
                entry.update(live_name=live.get("name"), live_ingest_ref=live.get("ingest_ref"))
                if r["ingest_ref"] and live.get("ingest_ref") != r["ingest_ref"]:
                    reasons.append("ref-not-carried")
            except Exception as e:  # noqa: BLE001 — reported, never acted on
                entry["live_unread"] = str(e)[:200]
            if not reasons:
                continue
            entry["reasons"] = reasons
            entry["patched"] = [
                {"ts": a[0], "detail": a[1]} for a in conn.execute(
                    "SELECT ts, detail FROM agent_activity WHERE worker = 'entity-sync' "
                    "AND action = 'synced' AND target = ? AND detail NOT LIKE '%pushed -' "
                    "ORDER BY ts", (f"{kind}:{r['entity_uid']}",))]
            out.append(entry)
    return out
