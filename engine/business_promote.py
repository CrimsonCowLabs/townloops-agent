"""Operator-gated promote of business-discovery candidates.

Nothing a sweep found reaches TownLoops until the operator approves it:

  verified ──approve──▶ approved ──promote run──▶ promoted (shipped)
      │                    │                 └──▶ already-live (live by now)
      └──────decline───────┴──▶ rejected ('operator…'; remembered by name)

`approve` / `decline` are status flips the operator triggers — the
dashboard's per-row buttons, or a chat Confirm widget
(`promote_businesses` / `decline_businesses` proposals from `propose`,
executed by dashboard/intent_handlers.py, which Hermes can't reach).

`promote_approved` takes only `approved` rows: it checks live again
(website / ≥0.85 name, as at sweep time), makes the local business (or
reuses an unshipped local one of the same name), enriches it, and ships it
through engine/entity_ship — the same create-or-adopt + sync path event-ship
uses. A ship that fails or an enrichment that couldn't read the site leaves
the row `approved` for the next run.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable, Iterable, Literal, Optional
from urllib.parse import urlparse

from engine import business_discovery as bd
from engine import entity_ship
from engine.derive_helpers import (
    best_name_match, insert_entity, iso_utc_now, mint_entity_uid,
)
from engine.entity_enrich import SPECS
from engine.entity_sync import refresh_content_hash


# enrich(conn, entity_uid) → entity_enrich status ('enriched', 'not-found',
# 'deferred', 'skipped').
Enrich = Callable[[sqlite3.Connection, str], str]

ProposalKind = Literal["promote", "decline"]
_PROPOSAL_KINDS = {"promote": "promote_businesses", "decline": "decline_businesses"}
# What each proposal can act on.
_PROPOSABLE = {"promote": ("verified",), "decline": ("new", "verified", "approved")}

# What an imported research draft (`verify.draft`) may fill.
_DRAFT_FIELDS = (*SPECS[bd.KIND].fillable, "category_slug")

# Social pages ship as that link, not as the business website.
_SOCIAL_HOSTS = {"facebook.com": "facebook", "instagram.com": "instagram", "tiktok.com": "tiktok"}


# --------------------------------------------------------------------------
# Operator decisions
# --------------------------------------------------------------------------

def approve(conn: sqlite3.Connection, uids: Iterable[str]) -> dict:
    """verified → approved. Anything else (unverified, declined, already
    live or promoted) is skipped."""
    return _flip(conn, uids, "approved", ("verified",), None, "approved")


def decline(conn: sqlite3.Connection, uids: Iterable[str], note: Optional[str] = None) -> dict:
    """→ rejected with an `operator` reason, which is what later sweeps
    remember (by URL, and by name via business_discovery.declined_match).
    Still-open candidates of the same business under other URLs (its site
    and its Facebook page, say) are declined with it."""
    reason = bd.OPERATOR_REASON + (f": {note.strip()}" if note and note.strip() else "")
    out = _flip(conn, uids, "rejected", _PROPOSABLE["decline"], reason, "declined")
    twins = 0
    for uid in set(uids) - set(out["skipped_uids"]):
        row = conn.execute("SELECT town_slug, display_name FROM discovery_candidates "
                           "WHERE candidate_uid = ?", (uid,)).fetchone()
        open_rows = [r for s in _PROPOSABLE["decline"]
                     for r in bd.list_candidates(conn, row[0], status=s, limit=10_000)]
        same = [r["candidate_uid"] for r in open_rows
                if bd.declined_match(conn, row[0], r["display_name"])]
        twins += _flip(conn, same, "rejected", _PROPOSABLE["decline"],
                       f"{bd.OPERATOR_REASON}: declined with {row[1]!r} ({uid[:12]})",
                       "declined")["declined"]
    return {**out, "twins_declined": twins}


def _flip(conn: sqlite3.Connection, uids: Iterable[str], status: str,
          from_statuses: tuple[str, ...], reason: Optional[str], counted_as: str) -> dict:
    """Set `status` (+ reject_reason) on each candidate currently in one of
    `from_statuses`; the result counts them under `counted_as`."""
    done, skipped = 0, []
    marks = ", ".join("?" * len(from_statuses))
    for uid in uids:
        cur = conn.execute(
            f"UPDATE discovery_candidates SET status = ?, reject_reason = ?, last_seen_at = ? "
            f"WHERE candidate_uid = ? AND kind = ? AND status IN ({marks})",
            (status, reason, iso_utc_now(), uid, bd.KIND, *from_statuses),
        )
        if cur.rowcount:
            done += 1
        else:
            skipped.append(uid)
    conn.commit()
    return {counted_as: done, "skipped": len(skipped), "skipped_uids": skipped}


def propose(conn: sqlite3.Connection, town_slug: str, kind: ProposalKind,
            uids: Optional[list[str]] = None, limit: int = 50) -> dict:
    """A read-only proposal for the chat Confirm widget: the candidates
    `kind` could act on — all of them, or those whose uid starts with one
    of `uids`. `proposal` is None when nothing matches."""
    statuses = _PROPOSABLE[kind]
    rows = [r for s in statuses
            for r in bd.list_candidates(conn, town_slug, status=s, limit=10_000)]
    if uids:
        rows = [r for r in rows if any(r["candidate_uid"].startswith(u) for u in uids if u)]
    rows = rows[:limit]
    preview = [{"candidate_uid": r["candidate_uid"], "name": r["display_name"],
                "url": r["canonical_url"], "status": r["status"],
                **{k: (r["verify"] or {}).get(k) for k in ("address", "phone")
                   if (r["verify"] or {}).get(k)}}
               for r in rows]
    if not rows:
        return {"proposal": None, "preview": []}
    return {"proposal": {"kind": _PROPOSAL_KINDS[kind],
                         "uids": [r["candidate_uid"] for r in rows], "count": len(rows)},
            "preview": preview}


# --------------------------------------------------------------------------
# Promote run
# --------------------------------------------------------------------------

def promote_approved(conn: sqlite3.Connection, client, town_slug: str, *,
                     enrich: Enrich, limit: int = 3) -> dict:
    """Promote up to `limit` approved candidates (see module doc). The live
    business list is only read when there is something to promote."""
    out = {"processed": 0, "promoted": 0, "already_live": 0, "deferred": 0, "failed": 0,
           "withdrawn": 0,
           "shipped": [], "errors": []}
    approved = bd.approved_queue(conn, town_slug, limit)
    if not approved:
        return out
    index = bd.LiveIndex.from_api(client)
    for cand in approved:
        out["processed"] += 1
        uid, url = cand["candidate_uid"], cand["canonical_url"]
        name = cand["display_name"] or bd.name_from_title((cand["verify"] or {}).get("title"))
        if not name:
            out["failed"] += 1
            _failed(conn, out, uid, f"{url}: no business name — decline it or name it first")
            continue
        live = index.match(url, name)
        if live:
            _advance(conn, uid, "already-live", live_slug=live.slug, reject_reason=live.reason)
            out["already_live"] += 1
            continue
        entity_uid = _local_business(conn, cand, name)
        _advance(conn, uid, "approved", research_entity_uid=entity_uid)
        try:
            status = enrich(conn, entity_uid)
        except Exception as e:  # noqa: BLE001 — the next run retries
            status, why = "deferred", f"enrich failed: {type(e).__name__}: {e}"
        else:
            why = "enrichment couldn't read the site"
        if status == "deferred":
            out["deferred"] += 1
            _failed(conn, out, uid, f"{url}: {why} — retried next run"[:300])
            continue
        if not _still_approved(conn, uid):
            # The operator declined it while it was being enriched.
            out["withdrawn"] += 1
            continue
        slug = entity_ship.ship_entity(client, conn, bd.KIND, entity_uid)
        if not slug:
            out["failed"] += 1
            _failed(conn, out, uid, f"{url}: ship failed — stays approved for the next run")
            continue
        cross_town = entity_ship.cross_town(conn, bd.KIND, entity_uid)
        _advance(conn, uid, "promoted", live_slug=slug,
                 promote_error=f"filed under town {cross_town!r}" if cross_town else None)
        if cross_town:
            out["errors"].append(f"{url}: shipped as {slug}, but TownLoops filed it under "
                                 f"town {cross_town!r} — check it in the admin")
        out["promoted"] += 1
        out["shipped"].append({"candidate_uid": uid, "name": name, "slug": slug})
    return out


def _still_approved(conn: sqlite3.Connection, uid: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM discovery_candidates WHERE candidate_uid = ? "
                             "AND status = 'approved'", (uid,)).fetchone())


def _failed(conn: sqlite3.Connection, out: dict, uid: str, msg: str) -> None:
    """Record a failed attempt: it stays `approved`, moves behind candidates
    with fewer attempts, and the operator can see why."""
    out["errors"].append(msg)
    conn.execute("UPDATE discovery_candidates SET promote_attempts = promote_attempts + 1, "
                 "promote_error = ? WHERE candidate_uid = ? AND status = 'approved'", (msg, uid))
    conn.commit()


def _advance(conn: sqlite3.Connection, uid: str, status: str, **cols) -> None:
    """Move an `approved` candidate on — never one the operator declined
    while the run was going."""
    sets = ", ".join(f"{k} = ?" for k in ("status", *cols, "last_seen_at"))
    conn.execute(f"UPDATE discovery_candidates SET {sets} "
                 "WHERE candidate_uid = ? AND status = 'approved'",
                 (status, *cols.values(), iso_utc_now(), uid))
    conn.commit()


def _local_business(conn: sqlite3.Connection, cand: dict, name: str) -> str:
    """The local business for this candidate: the one an earlier run made,
    else an unshipped local business of the same name (event mining may
    have made it), else a new one. The candidate's page and verified
    details — and the fields of a research draft it was imported from
    (engine/cmo_import) — fill only blanks."""
    entity_uid = cand.get("research_entity_uid")
    if not entity_uid:
        unshipped = [dict(r) for r in conn.execute(
            "SELECT * FROM businesses WHERE townloops_slug IS NULL AND source_kind != 'live'")]
        same = best_name_match(name, unshipped)
        entity_uid = same["entity_uid"] if same else mint_entity_uid(
            bd.KIND, name, f"candidate:{cand['candidate_uid']}")
    verify = cand["verify"] or {}
    fields = {**{k: v for k, v in (verify.get("draft") or {}).items() if k in _DRAFT_FIELDS},
              _page_field(cand["canonical_url"]): cand["canonical_url"],
              **{k: verify[k] for k in ("address", "phone") if verify.get(k)}}
    if not conn.execute("SELECT 1 FROM businesses WHERE entity_uid = ?", (entity_uid,)).fetchone():
        insert_entity(conn, "businesses", entity_uid, {
            "source": f"business-discovery:{cand.get('source_signal') or 'sweep'}",
            "source_kind": "discovery", "name": name,
            "source_url": cand["canonical_url"],
            "extra_json": json.dumps({"candidate_uid": cand["candidate_uid"],
                                      **({"hours": verify["hours"]} if verify.get("hours") else {})}),
        })
    sets = ", ".join(f"{k} = COALESCE(NULLIF({k}, ''), ?)" for k in fields)
    conn.execute(f"UPDATE businesses SET {sets} WHERE entity_uid = ?",
                 (*fields.values(), entity_uid))
    refresh_content_hash(conn, bd.KIND, entity_uid)
    conn.commit()
    return entity_uid


def _page_field(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    return _SOCIAL_HOSTS.get(host, "website")
