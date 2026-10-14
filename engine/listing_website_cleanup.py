"""Clear listing-page websites (engine/listing_hosts) local and live, then
re-enrich — working from the audit's rows (engine/listing_website_audit).

Both sides, or the clear is undone: entity_sync's resync copies live values
into local rows (it never copies a listing-page website either way, so a
clear holds even while live still shows one).

Supervised (default) — nothing changes without the operator:

    propose (read-only, the chat Confirm widget's `clear_listing_websites`)
      ──operator confirms──▶ confirmed ──apply──▶ cleared | failed | resolved

  `apply` clears live first — a full-body PATCH with `website: ""`, GET
  verified (engine/live_patch.clear_entity_fields) — and only then local. A
  PATCH that doesn't land parks the row `failed` (local untouched) until the
  operator confirms it again.

Autonomous — a published value is never PATCHed:

    run ──▶ submitted (a pending copy of the live row without its website,
            for the TownLoops merge flow) ──moderation poll──▶ cleared |
            not-accepted

  The local clear follows only once the target shows no listing website
  (`settle`, from the moderation poll and each run). A listing website that
  is local only (live is right, or not shipped) is no published value: it is
  cleared locally at once. Each target is tried once; `not-accepted` isn't
  resubmitted.

A local clear keeps the listing URL as research evidence
(`extra_json.listing_pages`), takes live's own site when live has one,
else blanks `website` and sets `enrichment_status` 'pending' so `reenrich`
(the injected entity_enrich call) looks for the entity's own site — which
then reaches live as a fill-blank (entity_sync). Live mirrors aren't
researched (their values are TownLoops'). Rows the audit couldn't read live
(`live_unread`) are never cleared — skipped and reported. Every row says
what `changed`: 'live', 'local', 'both' or 'none'.

Ledger: `listing_website_clears` (migration 026), one row per target —
'<kind>:<entity_uid>', or '<kind>#<live id>' for a live row with no local
row.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Callable, Iterable, Optional

from engine.derive_helpers import (
    dict_rows as _rows, iso_utc_now, parse_extra_json, record_activity,
)
from engine.entity_classify import TABLES as KINDS   # kind → resource / table
from engine.listing_hosts import is_listing_url


PROPOSAL_KIND = "clear_listing_websites"
CLEAR_KIND = "clear-website"          # submissions.kind of an autonomous clear
EVIDENCE_KEY = "listing_pages"        # extra_json: listing URLs once held as website
_WORKER = "listing-website-cleanup"
# Statuses a target is done with until the operator confirms it again.
_OPEN = ("confirmed", "submitted")

# enrich(conn, kind, entity_uid) → an entity_enrich.EnrichOutcome status.
Enrich = Callable[[sqlite3.Connection, str, str], str]


def target_of(row: dict) -> str:
    """The audit row's ledger/proposal key."""
    if row.get("entity_uid"):
        return f"{row['kind']}:{row['entity_uid']}"
    return f"{row['kind']}#{row['live_id']}"


def _clearable(row: dict) -> bool:
    return not row["live_unread"] and (row["entity_uid"] or row["live_id"] is not None)


# --------------------------------------------------------------------------
# Supervised: propose → operator confirms → apply
# --------------------------------------------------------------------------

def propose(conn: sqlite3.Connection, client, targets: Optional[list[str]] = None,
            limit: int = 50) -> dict:
    """A read-only proposal for the chat Confirm widget: every clearable
    audit row — or those whose target, entity uid or name starts with one
    of `targets`.
    Rows whose live side couldn't be read are listed in `skipped`, never
    proposed. `proposal` is None when nothing matches."""
    from engine.listing_website_audit import find_listing_websites
    found = find_listing_websites(conn, client)
    rows, skipped = [], []
    for r in found.rows:
        if targets and not any(t and ((_clearable(r) and target_of(r).startswith(t))
                                      or (r["entity_uid"] or "").startswith(t)
                                      or (r["name"] or "").lower().startswith(t.lower()))
                               for t in targets):
            continue
        (rows if _clearable(r) else skipped).append(r)
    rows = rows[:limit]
    skipped = [{"kind": r["kind"], "name": r["name"], "entity_uid": r["entity_uid"],
                "live_slug": r["live_slug"], "why": "live side unread — re-run later"}
               for r in skipped]
    if not rows:
        return {"proposal": None, "preview": [], "skipped": skipped}
    return {"proposal": {"kind": PROPOSAL_KIND, "uids": [target_of(r) for r in rows],
                         "count": len(rows)},
            "preview": [preview(r) for r in rows], "skipped": skipped}


def preview(row: dict) -> dict:
    return {"target": target_of(row), "kind": row["kind"], "name": row["name"],
            "side": row["side"], "url": row["url"], "live_slug": row["live_slug"]}


def confirm(conn: sqlite3.Connection, targets: Iterable[str]) -> dict:
    """The operator confirmed these clears: the next `apply` makes them. A
    target already confirmed or submitted is skipped."""
    done, skipped = 0, []
    now = iso_utc_now()
    for t in targets:
        kind, uid, _ = _parse_target(t)
        if kind is None or (uid and not conn.execute(
                f"SELECT 1 FROM {KINDS[kind]} WHERE entity_uid = ?", (uid,)).fetchone()):
            skipped.append(t)
            continue
        cur = conn.execute(
            "INSERT INTO listing_website_clears (target, kind, entity_uid, status, created_at, "
            "  updated_at) VALUES (?, ?, ?, 'confirmed', ?, ?) "
            "ON CONFLICT (target) DO UPDATE SET status = 'confirmed', note = NULL, "
            "  updated_at = excluded.updated_at "
            f"WHERE listing_website_clears.status NOT IN ({','.join('?' * len(_OPEN))})",
            (t, kind, uid, now, now, *_OPEN))
        if cur.rowcount:
            done += 1
        else:
            skipped.append(t)
    conn.commit()
    return {"confirmed": done, "skipped": len(skipped), "skipped_uids": skipped}


def _parse_target(t: str) -> tuple[Optional[str], Optional[str], Optional[int]]:
    if not isinstance(t, str):
        return None, None, None
    for sep in (":", "#"):
        kind, found, rest = t.partition(sep)
        if found and kind in KINDS and rest:
            if sep == ":":
                return kind, rest, None
            return (kind, None, int(rest)) if rest.isdigit() else (None, None, None)
    return None, None, None


def apply_confirmed(conn: sqlite3.Connection, client, limit: int = 20) -> list[dict]:
    """Clear what the operator confirmed, live first. Returns one report row
    per target touched."""
    confirmed = _rows(conn, "SELECT * FROM listing_website_clears WHERE status = 'confirmed' "
                            "ORDER BY updated_at, target LIMIT ?", (limit,))
    if not confirmed:
        return []
    from engine.listing_website_audit import find_listing_websites
    found = {target_of(r): r for r in find_listing_websites(conn, client).rows}
    out = []
    for led in confirmed:
        row = found.get(led["target"])
        if row is None:
            out.append(_set(conn, led["target"], "resolved", "none",
                            note="no listing website any more — nothing to clear"))
        elif row["live_unread"]:
            # Live side unknown: stays confirmed for the next run.
            out.append({"target": led["target"], "status": "confirmed", "changed": "none",
                        "note": "live side unread — retried next run"})
        else:
            out.append(_clear_supervised(conn, client, row))
    return out


def _clear_supervised(conn: sqlite3.Connection, client, row: dict) -> dict:
    from engine.live_patch import clear_entity_fields
    target, resource = target_of(row), KINDS[row["kind"]]
    live_cleared = False
    if row["side"] in ("live", "both"):
        try:
            res = clear_entity_fields(client, resource, row["live_slug"], ("website",))
        except Exception as e:  # noqa: BLE001 — parked, not retried blind
            return _set(conn, target, "failed", "none", row=row,
                        note=f"live clear failed: {str(e)[:300]}")
        if res.mismatches:
            return _set(conn, target, "failed", "none", row=row,
                        note="live clear didn't land: " + "; ".join(res.mismatches)[:500])
        live_cleared = True
    local = _clear_local(conn, row, live_verified=live_cleared) if row["entity_uid"] else None
    rep = _set(conn, target, "cleared", _changed(live_cleared, local), row=row,
               reenrich=(local or {}).get("reenrich"), note=(local or {}).get("note"))
    record_activity(conn, _WORKER, "cleared", target,
                    detail=f"{row['name']}: {row['url']} — changed {rep['changed']}")
    conn.commit()
    return rep


# --------------------------------------------------------------------------
# Local clear (both modes)
# --------------------------------------------------------------------------

def _clear_local(conn: sqlite3.Connection, row: dict, *, live_verified: bool,
                 live_website: Optional[str] = None) -> Optional[dict]:
    """Take the listing website off the local row: live's own site when it
    has one (`live_website`, else the audit row's), else blank + re-enrich.
    Keeps the listing URL as evidence. `live_verified`: live was just
    cleared and GET-verified, so a row that was in sync stays in sync.
    None when the local website is no listing (nothing changed)."""
    from engine.entity_sync import refresh_content_hash
    kind, uid = row["kind"], row["entity_uid"]
    table = KINDS[kind]
    found = _rows(conn, f"SELECT * FROM {table} WHERE entity_uid = ?", (uid,))
    if not found or not is_listing_url(found[0]["website"]):
        return None
    cur = found[0]
    live = row["live_website"] if live_website is None else live_website
    own = live if live and not is_listing_url(live) and not row.get("duplicate_of") else None
    extra = parse_extra_json(cur.get("extra_json"))
    pages = list(extra.get(EVIDENCE_KEY) or [])
    for u in (cur["website"], row.get("live_website")):
        if is_listing_url(u) and u not in pages:
            pages.append(u)
    extra[EVIDENCE_KEY] = pages
    mirror = cur.get("source_kind") == "live"
    queue = own is None and not mirror
    status = "pending" if queue else cur.get("enrichment_status")
    conn.execute(f"UPDATE {table} SET website = ?, extra_json = ?, enrichment_status = ?, "
                 "last_modified_at = ? WHERE entity_uid = ?",
                 (own, json.dumps(extra, ensure_ascii=False), status, iso_utc_now(), uid))
    in_sync = cur.get("pushed_hash") is not None and cur["pushed_hash"] == cur.get("content_hash")
    refresh_content_hash(conn, kind, uid)
    if live_verified and in_sync:
        conn.execute(f"UPDATE {table} SET pushed_hash = content_hash WHERE entity_uid = ?", (uid,))
    if own:
        return {"reenrich": "skipped", "note": f"took live's own site {own}"}
    if mirror:
        return {"reenrich": "skipped", "note": "live mirror — not researched locally"}
    return {"reenrich": "queued", "note": None}


def _changed(live: bool, local: Optional[dict]) -> str:
    return {(True, True): "both", (True, False): "live",
            (False, True): "local"}.get((live, local is not None), "none")


def _set(conn: sqlite3.Connection, target: str, status: str, changed: str, *,
         row: Optional[dict] = None, note: Optional[str] = None,
         reenrich: Optional[str] = None, pending_slug: Optional[str] = None) -> dict:
    """Upsert the target's ledger row; returns its report row."""
    kind, uid, _ = _parse_target(target)
    now = iso_utc_now()
    vals = {"status": status, "changed": changed, "note": note, "updated_at": now}
    if row is not None:
        vals.update(live_slug=row.get("live_slug"), url=row.get("url"))
    if reenrich is not None:
        vals["reenrich"] = reenrich
    if pending_slug is not None:
        vals["pending_slug"] = pending_slug
    conn.execute(
        "INSERT INTO listing_website_clears (target, kind, entity_uid, status, created_at, "
        "  updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (target) DO NOTHING",
        (target, kind, uid, status, now, now))
    conn.execute(f"UPDATE listing_website_clears SET {', '.join(f'{k} = ?' for k in vals)} "
                 "WHERE target = ?", (*vals.values(), target))
    return report_row(_rows(conn, "SELECT * FROM listing_website_clears WHERE target = ?",
                            (target,))[0], name=(row or {}).get("name"))


def report_row(led: dict, name: Optional[str] = None) -> dict:
    return {"target": led["target"], "name": name, "url": led["url"],
            "live_slug": led["live_slug"], "status": led["status"],
            "changed": led["changed"], "reenrich": led["reenrich"], "note": led["note"]}


def ledger(conn: sqlite3.Connection) -> list[dict]:
    """Every target, newest change first, with the entity's name."""
    out = []
    for led in _rows(conn, "SELECT * FROM listing_website_clears ORDER BY updated_at DESC, target"):
        name = None
        if led["entity_uid"]:
            r = next(iter(_rows(conn, f"SELECT name, website FROM {KINDS[led['kind']]} "
                                      "WHERE entity_uid = ?", (led["entity_uid"],))), None)
            name = r["name"] if r else None
            out.append({**report_row(led, name), "website": r["website"] if r else None})
        else:
            out.append(report_row(led))
    return out


# --------------------------------------------------------------------------
# Autonomous: submit for the merge flow, settle on the moderator's outcome
# --------------------------------------------------------------------------

def submit_found(conn: sqlite3.Connection, client, limit: int = 10) -> dict:
    """Autonomous: each listing website the audit finds and no earlier try
    covers — a live one goes to the moderator as a pending copy of the live
    row without its website (`client` is an autonomy.GatedClient); a local
    one is cleared locally. A held gate stops the pass."""
    from engine import autonomy
    from engine.listing_website_audit import find_listing_websites
    out = {"submitted": [], "cleared": [], "skipped": [], "held": None, "errors": []}
    tried = {r[0] for r in conn.execute(
        "SELECT target FROM listing_website_clears WHERE status != 'resolved'")}
    n = 0
    for row in find_listing_websites(conn, client).rows:
        if not _clearable(row):
            if row["live_unread"]:
                out["skipped"].append({"name": row["name"], "why": "live side unread"})
            continue
        target = target_of(row)
        if target in tried or n >= limit:
            continue
        n += 1
        if row["side"] == "local":
            local = _clear_local(conn, row, live_verified=False)
            rep = _set(conn, target, "cleared", _changed(False, local), row=row,
                       reenrich=(local or {}).get("reenrich"), note=(local or {}).get("note"))
            autonomy.record(conn, "listing-website", target, autonomy.Decision(
                "clear-local", "listing-page-website", {"url": row["url"]}))
            out["cleared"].append(rep)
            conn.commit()
            continue
        try:
            rep = _submit(conn, client, row)
        except autonomy.SubmissionsHeld as e:
            out["held"] = e.reason
            break
        except Exception as e:  # noqa: BLE001 — tried again next run
            out["errors"].append(f"{target}: {str(e)[:200]}")
            continue
        if rep.get("status") == "submitted":
            out["submitted"].append(rep)
        else:
            out["skipped"].append(rep)
        conn.commit()
    return out


def _submit(conn: sqlite3.Connection, client, row: dict) -> dict:
    from engine import autonomy
    from engine.live_lookup import is_conflict
    from engine.live_patch import sendable
    target, resource = target_of(row), KINDS[row["kind"]]
    live = client.get(resource, row["live_slug"]) or {}
    if live.get("status") not in (None, "published"):
        return {"target": target, "name": row["name"],
                "why": f"live row is {live.get('status')}, not published"}
    payload = {k: v for k, v in live.items() if k != "website" and sendable(resource, k, v)}
    payload["website"] = None
    town = os.environ.get("TOWNLOOPS_TOWN_SLUG", "unknown")
    key = hashlib.sha1(target.encode()).hexdigest()
    payload["slug"] = f"hermes-{town}-clearweb-{key[:12]}"
    try:
        created = client.create(resource, payload,
                                idempotency_key=f"hermes-{town}:clear-website:{key[:16]}")
    except Exception as e:
        if not is_conflict(e):
            raise
        created = {"slug": payload["slug"]}
    slug = (created or {}).get("slug") or payload["slug"]
    autonomy.attribute(conn, resource, slug, kind=CLEAR_KIND, target_slug=row["live_slug"],
                       local_uid=row["entity_uid"] or target)
    autonomy.record(conn, "listing-website", target, autonomy.Decision(
        "submit", "listing-page-website",
        {"url": row["url"], "target_slug": row["live_slug"], "pending_slug": slug}))
    return _set(conn, target, "submitted", "none", row=row, pending_slug=slug,
                note=f"pending clear {slug} submitted for merge into {row['live_slug']}")


def read_target(client, resource: str, slug: Optional[str]) -> Optional[dict]:
    """The live row a clear was submitted for (or its merge survivor): {}
    when it's gone, None when it can't be read."""
    from engine.live_lookup import find_live_entity
    try:
        return find_live_entity(client, resource, slug) or {}
    except Exception:  # noqa: BLE001 — unanswered
        return None


def shows_clear(live: Optional[dict]) -> bool:
    """Whether a target read shows the clear: a live row with no listing website."""
    return bool(live) and not is_listing_url(live.get("website"))


def settle(conn: sqlite3.Connection, client) -> list[dict]:
    """Autonomous: each submitted clear the moderator has decided. Accepted —
    the target no longer shows a listing website — clears local; otherwise
    `not-accepted`. A target that can't be read is settled next time."""
    from engine import autonomy
    out = []
    for led in _rows(conn, "SELECT l.*, s.status AS outcome, s.resource FROM "
                           "listing_website_clears l JOIN submissions s ON s.slug = l.pending_slug "
                           "AND s.kind = ? WHERE l.status = 'submitted' AND s.status != 'pending'",
                     (CLEAR_KIND,)):
        target = led["target"]
        if led["outcome"] == "rejected":
            rep = _set(conn, target, "not-accepted", "none", note="moderator rejected the clear")
        else:
            live = read_target(client, led["resource"], led["live_slug"])
            if live is None:
                continue
            if not shows_clear(live):
                rep = _set(conn, target, "not-accepted", "none",
                           note=f"moderator {led['outcome']}; live still shows a listing website")
            else:
                local = None
                if led["entity_uid"]:
                    row = {"kind": led["kind"], "entity_uid": led["entity_uid"],
                           "live_website": None, "duplicate_of": None}
                    local = _clear_local(conn, row, live_verified=True,
                                         live_website=live.get("website") or "")
                rep = _set(conn, target, "cleared", _changed(True, local),
                           reenrich=(local or {}).get("reenrich"), note=(local or {}).get("note"))
        autonomy.record(conn, "listing-website", target, autonomy.Decision(
            rep["status"], f"moderator-{led['outcome']}",
            {"pending_slug": led["pending_slug"], "target_slug": led["live_slug"],
             "changed": rep["changed"]}))
        conn.commit()
        out.append(rep)
    return out


# --------------------------------------------------------------------------
# Re-enrichment + the run
# --------------------------------------------------------------------------

def reenrich(conn: sqlite3.Connection, enrich: Enrich, limit: int = 10) -> list[dict]:
    """Look for the own site of each cleared entity still queued. One the
    lookup couldn't finish (budget, Firecrawl down) stays queued."""
    out = []
    for led in _rows(conn, "SELECT * FROM listing_website_clears WHERE reenrich = 'queued' "
                           "AND entity_uid IS NOT NULL ORDER BY updated_at, target LIMIT ?",
                     (limit,)):
        try:
            status = enrich(conn, led["kind"], led["entity_uid"])
        except Exception as e:  # noqa: BLE001 — stays queued
            status, note = "deferred", f"enrichment failed: {str(e)[:200]}"
        else:
            note = None
        website = conn.execute(f"SELECT website FROM {KINDS[led['kind']]} WHERE entity_uid = ?",
                               (led["entity_uid"],)).fetchone()
        if status != "deferred":
            conn.execute("UPDATE listing_website_clears SET reenrich = ?, updated_at = ? "
                         "WHERE target = ?", (status, iso_utc_now(), led["target"]))
        conn.commit()
        out.append({"target": led["target"], "reenrich": status,
                    "website": website[0] if website else None, **({"note": note} if note else {})})
    return out


def enricher(max_lookups: Optional[int] = None, llm_calls: int = 2) -> Enrich:
    """entity_enrich.enrich_entity under one shared budget, as
    event-enrich-entities: `max_lookups` Firecrawl calls
    ($TOWNLOOPS_ENRICH_MAX_LOOKUPS, default 8), `llm_calls` Codex calls
    within the next 100s."""
    import time
    from engine import entity_classify, entity_enrich
    from engine.derive_helpers import town_name
    if max_lookups is None:
        try:
            max_lookups = int(os.environ.get("TOWNLOOPS_ENRICH_MAX_LOOKUPS", 8))
        except ValueError:
            max_lookups = 8
    budget = entity_enrich.Budget(max_lookups)
    llm = entity_classify.codex_llm(llm_calls, 30, time.monotonic() + 100)
    town = town_name()
    return lambda conn, kind, uid: entity_enrich.enrich_entity(
        conn, kind, uid, town, budget, llm).status


def run(conn: sqlite3.Connection, client, *, enrich: Optional[Enrich] = None,
        limit: int = 20) -> dict:
    """One pass — the entity-live-sync cron's, or `apply`. Supervised: clear
    what the operator confirmed. Autonomous: settle decided submissions, then
    submit/clear what the audit finds. Then re-enrich. Without anything to
    do in supervised mode, no API call is made."""
    from engine import autonomy
    out: dict = {"mode": "autonomous" if autonomy.is_autonomous() else "supervised"}
    if out["mode"] == "autonomous":
        out["settled"] = settle(conn, client)
        out.update(submit_found(conn, autonomy.gated(client, conn), limit=limit))
    else:
        out["applied"] = apply_confirmed(conn, client, limit=limit)
    if enrich is not None:
        out["reenriched"] = reenrich(conn, enrich, limit=limit)
    return out
