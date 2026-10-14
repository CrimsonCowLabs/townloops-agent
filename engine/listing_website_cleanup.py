"""One-off cleanup of listing-page websites (engine/listing_hosts): clear
them and re-enrich, working from the audit's rows
(engine/listing_website_audit). Run only when the operator asks — no cron.

entity_sync's resync copies live values into local rows, but never a
listing-page website (in either direction), so a local clear holds even
while live still shows one.

Supervised (default) — nothing changes without the operator:

    propose (read-only, the chat Confirm widget's `clear_listing_websites`)
      ──operator confirms──▶ confirmed ──apply──▶ cleared | failed | resolved

  `apply` clears live first — a full-body PATCH with `website: ""`, GET
  verified (engine/live_patch.clear_entity_fields) — and only then local. A
  PATCH that doesn't land parks the row `failed` (local untouched) until the
  operator confirms it again.

Autonomous — a published value is never PATCHed, and TownLoops has no
entity change-submission endpoint yet:

    apply ──▶ cleared (local only)  |  flagged (live still holds a listing
              page: listed in the nightly digest for a fix in TownLoops
              admin) ──a later apply sees live fixed──▶ resolved

  The local listing website is cleared either way. `confirm` and `propose`
  are supervised-only (they answer inactive).

A local clear keeps the listing URL as research evidence
(`extra_json.listing_pages`), takes live's own site when live has one,
else blanks `website` and sets `enrichment_status` 'pending' so `reenrich`
(the injected entity_enrich call) looks for the entity's own site — which
reaches live as a fill-blank once live's website is blank. Live mirrors
(`source_kind='live'`) and live rows with no local row aren't researched:
enrich_entity never fills a mirror (its values are TownLoops'), so their
own site is a live fix, said in the row's note. Rows the audit couldn't
read live (`live_unread`) are never cleared — skipped and reported. Every
row says what `changed`: 'live', 'local', 'both' or 'none'.

Ledger: `listing_website_clears` (migration 026; its `pending_slug` column
is unused), one row per target — '<kind>:<entity_uid>', or '<kind>#<live
id>' for a live row with no local row. Status changes are optimistic
(`UPDATE ... WHERE status = <old>`; rowcount 0 → another run got there
first, skipped). Callers that write entity rows hold the `workqueue-drain`
lock, as entity-live-sync does.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable, Iterable, Optional

from engine.derive_helpers import dict_rows, iso_utc_now, parse_extra_json, record_activity
from engine.listing_hosts import is_listing_url
from engine.listing_website_audit import KINDS   # kind → resource / table


PROPOSAL_KIND = "clear_listing_websites"
EVIDENCE_KEY = "listing_pages"        # extra_json: listing URLs once held as website
LIVE_FIX_NOTE = "live still holds a listing page; fix in TownLoops admin"
_WORKER = "listing-website-cleanup"

# enrich(conn, kind, entity_uid) → an entity_enrich.EnrichOutcome status.
Enrich = Callable[[sqlite3.Connection, str, str], str]


def target_of(row: dict) -> str:
    """The audit row's ledger/proposal key."""
    if row.get("entity_uid"):
        return f"{row['kind']}:{row['entity_uid']}"
    return f"{row['kind']}#{row['live_id']}"


def _clearable(row: dict) -> bool:
    return not row["live_unread"] and bool(row["entity_uid"] or row["live_id"] is not None)


def _live_listing(row: dict) -> bool:
    return row["side"] in ("live", "both")


# --------------------------------------------------------------------------
# Supervised: propose → operator confirms → apply
# --------------------------------------------------------------------------

def propose(conn: sqlite3.Connection, client, targets: Optional[list[str]] = None,
            limit: int = 50) -> dict:
    """A read-only proposal for the chat Confirm widget: every clearable
    audit row — or those whose target, entity uid or name starts with one
    of `targets`. Rows whose live side couldn't be read are listed in
    `skipped`, never proposed. `proposal` is None when nothing matches."""
    from engine.listing_website_audit import find_listing_websites
    rows, skipped = [], []
    for r in find_listing_websites(conn, client).rows:
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
    target already confirmed is skipped. Supervised only — in autonomous
    mode nothing is recorded (apply wouldn't act on it)."""
    from engine import autonomy
    targets = list(targets)
    if autonomy.is_autonomous():
        return {"confirmed": 0, "skipped": len(targets), "skipped_uids": targets,
                **autonomy.inactive_message("clear_listing_websites")}
    done, skipped = 0, []
    now = iso_utc_now()
    for t in targets:
        kind, uid = _parse_target(t)
        if kind is None or (uid and not conn.execute(
                f"SELECT 1 FROM {KINDS[kind]} WHERE entity_uid = ?", (uid,)).fetchone()):
            skipped.append(t)
            continue
        cur = conn.execute(
            "INSERT INTO listing_website_clears (target, kind, entity_uid, status, created_at, "
            "  updated_at) VALUES (?, ?, ?, 'confirmed', ?, ?) "
            "ON CONFLICT (target) DO UPDATE SET status = 'confirmed', note = NULL, "
            "  updated_at = excluded.updated_at "
            "WHERE listing_website_clears.status != 'confirmed'",
            (t, kind, uid, now, now))
        if cur.rowcount:
            done += 1
        else:
            skipped.append(t)
    conn.commit()
    return {"confirmed": done, "skipped": len(skipped), "skipped_uids": skipped}


def _parse_target(t) -> tuple[Optional[str], Optional[str]]:
    """(kind, entity_uid) — entity_uid None for a '<kind>#<live id>' target;
    (None, None) for anything else."""
    if not isinstance(t, str):
        return None, None
    kind, sep, rest = t.partition(":")
    if sep and kind in KINDS and rest:
        return kind, rest
    kind, sep, rest = t.partition("#")
    if sep and kind in KINDS and rest.isdigit():
        return kind, None
    return None, None


def apply_confirmed(conn: sqlite3.Connection, client, limit: int = 20) -> list[dict]:
    """Clear what the operator confirmed, live first. Returns one report row
    per target touched."""
    confirmed = dict_rows(conn, "SELECT * FROM listing_website_clears WHERE status = 'confirmed' "
                                "ORDER BY updated_at, target LIMIT ?", (limit,))
    if not confirmed:
        return []
    from engine.listing_website_audit import find_listing_websites
    found = {target_of(r): r for r in find_listing_websites(conn, client).rows}
    out = []
    for led in confirmed:
        row = found.get(led["target"])
        if row is None:
            rep = _finish(conn, led["target"], "confirmed", "resolved", "none",
                          note="no listing website any more — nothing to clear")
        elif row["live_unread"]:
            # Live side unknown: stays confirmed for the next apply.
            rep = {"target": led["target"], "status": "confirmed", "changed": "none",
                   "note": "live side unread — retried next apply"}
        else:
            rep = _clear_supervised(conn, client, row)
        if rep:
            out.append(rep)
    return out


def _clear_supervised(conn: sqlite3.Connection, client, row: dict) -> Optional[dict]:
    from engine.live_patch import clear_entity_fields
    target, resource = target_of(row), KINDS[row["kind"]]
    live_cleared = False
    if _live_listing(row):
        try:
            res = clear_entity_fields(client, resource, row["live_slug"], ("website",))
        except Exception as e:  # noqa: BLE001 — parked, not retried blind
            return _finish(conn, target, "confirmed", "failed", "none", row=row,
                           note=f"live clear failed: {str(e)[:300]}")
        if res.mismatches:
            return _finish(conn, target, "confirmed", "failed", "none", row=row,
                           note="live clear didn't land: " + "; ".join(res.mismatches)[:500])
        live_cleared = True
    rep = _finish(conn, target, "confirmed", "cleared", row=row, live_changed=live_cleared,
                  live_verified=live_cleared)
    if rep:
        record_activity(conn, _WORKER, "cleared", target,
                        detail=f"{row['name']}: {row['url']} — changed {rep['changed']}")
        conn.commit()
    return rep


# --------------------------------------------------------------------------
# Autonomous: clear local, flag live
# --------------------------------------------------------------------------

def run_autonomous(conn: sqlite3.Connection, client, limit: int = 50) -> dict:
    """Clear every listing website the audit finds locally; a live one is
    `flagged` for TownLoops admin, never PATCHed. A target already handled
    is left alone; a flagged one whose live row no longer shows a listing
    page is `resolved`."""
    from engine import autonomy
    from engine.listing_website_audit import find_listing_websites
    out: dict = {"cleared": [], "flagged": [], "resolved": [], "skipped": []}
    ledger_status = {r["target"]: r["status"] for r in dict_rows(
        conn, "SELECT target, status FROM listing_website_clears")}
    rows = find_listing_websites(conn, client).rows
    still_live, unread = set(), set()
    n = 0
    for row in rows:
        if row["live_unread"]:
            unread.add(target_of(row))
            out["skipped"].append({"name": row["name"], "why": "live side unread"})
            continue
        if not _clearable(row):
            continue
        target = target_of(row)
        if _live_listing(row):
            still_live.add(target)
        old = ledger_status.get(target)
        if old not in (None, "confirmed", "resolved") or n >= limit:
            continue
        n += 1
        new = "flagged" if _live_listing(row) else "cleared"
        rep = _finish(conn, target, old, new, row=row, live_changed=False, live_verified=False,
                      flag_note=LIVE_FIX_NOTE if new == "flagged" else None)
        if rep is None:
            continue
        autonomy.record(conn, "listing-website", target, autonomy.Decision(
            "flag-live" if new == "flagged" else "clear-local", "listing-page-website",
            {"url": row["url"], "changed": rep["changed"], "live_slug": row["live_slug"]}))
        conn.commit()
        out[new].append(rep)
    for target, status in ledger_status.items():
        if status == "flagged" and target not in still_live and target not in unread:
            rep = _finish(conn, target, "flagged", "resolved", "none",
                          note="live no longer shows a listing page")
            if rep:
                out["resolved"].append(rep)
    return out


def flagged(conn: sqlite3.Connection) -> list[dict]:
    """Targets whose live row still held a listing page when last looked at
    — the nightly digest lists them until an apply sees live fixed."""
    return [r for r in ledger(conn) if r["status"] == "flagged"]


# --------------------------------------------------------------------------
# Ledger transitions + the local clear
# --------------------------------------------------------------------------

def _finish(conn: sqlite3.Connection, target: str, old: Optional[str], new: str,
            changed: Optional[str] = None, *, row: Optional[dict] = None,
            note: Optional[str] = None, live_changed: bool = False,
            live_verified: bool = False, flag_note: Optional[str] = None) -> Optional[dict]:
    """Move `target` from `old` (None: no ledger row yet) to `new`, in one
    transaction with the local clear when `changed` is None (computed from
    it and `live_changed`). None when another run moved it first."""
    kind, uid = _parse_target(target)
    now = iso_utc_now()
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if old is None:
            cur = conn.execute(
                "INSERT INTO listing_website_clears (target, kind, entity_uid, status, "
                "  created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (target) DO NOTHING", (target, kind, uid, new, now, now))
        else:
            cur = conn.execute("UPDATE listing_website_clears SET status = ?, updated_at = ? "
                               "WHERE target = ? AND status = ?", (new, now, target, old))
        if not cur.rowcount:
            conn.execute("ROLLBACK")
            return None
        local = None
        if changed is None:
            local = _clear_local(conn, row, live_verified=live_verified) if uid else None
            changed = _changed(live_changed, local)
            note = "; ".join(x for x in ((local or {}).get("note"), flag_note) if x) or None
            if uid is None and new == "cleared":
                note = "no local row; set its own site in TownLoops admin"
        vals = {"changed": changed, "note": note}
        if row is not None:
            vals.update(live_slug=row.get("live_slug"), url=row.get("url"))
        if local is not None:
            vals["reenrich"] = local["reenrich"]
        conn.execute(f"UPDATE listing_website_clears SET {', '.join(f'{k} = ?' for k in vals)} "
                     "WHERE target = ?", (*vals.values(), target))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    led = dict_rows(conn, "SELECT * FROM listing_website_clears WHERE target = ?", (target,))[0]
    return report_row(led, name=(row or {}).get("name"))


def _clear_local(conn: sqlite3.Connection, row: dict, *, live_verified: bool) -> Optional[dict]:
    """Take the listing website off the local row: live's own site when it
    has one, else blank + re-enrich. Keeps the listing URL as evidence.
    `live_verified`: live was just cleared and GET-verified, so a row that
    was in sync stays in sync. None when the local website is no listing
    (nothing changed). Caller holds the transaction."""
    from engine.entity_sync import refresh_content_hash
    kind, uid = row["kind"], row["entity_uid"]
    table = KINDS[kind]
    found = dict_rows(conn, f"SELECT * FROM {table} WHERE entity_uid = ?", (uid,))
    if not found or not is_listing_url(found[0]["website"]):
        return None
    cur = found[0]
    live = row.get("live_website")
    own = live if live and not is_listing_url(live) and not row.get("duplicate_of") else None
    extra = parse_extra_json(cur.get("extra_json"))
    pages = list(extra.get(EVIDENCE_KEY) or [])
    for u in (cur["website"], live):
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
        return {"reenrich": "skipped",
                "note": "live mirror — not researched locally; set its own site in TownLoops admin"}
    return {"reenrich": "queued", "note": None}


def _changed(live: bool, local: Optional[dict]) -> str:
    return {(True, True): "both", (True, False): "live",
            (False, True): "local"}.get((live, local is not None), "none")


def report_row(led: dict, name: Optional[str] = None) -> dict:
    return {"target": led["target"], "name": name, "url": led["url"],
            "live_slug": led["live_slug"], "status": led["status"],
            "changed": led["changed"], "reenrich": led["reenrich"], "note": led["note"]}


def ledger(conn: sqlite3.Connection) -> list[dict]:
    """Every target, newest change first, with the entity's name."""
    out = []
    for led in dict_rows(conn, "SELECT * FROM listing_website_clears "
                               "ORDER BY updated_at DESC, target"):
        if led["entity_uid"]:
            r = next(iter(dict_rows(conn, f"SELECT name, website FROM {KINDS[led['kind']]} "
                                          "WHERE entity_uid = ?", (led["entity_uid"],))), None)
            out.append({**report_row(led, r["name"] if r else None),
                        "website": r["website"] if r else None})
        else:
            out.append(report_row(led))
    return out


# --------------------------------------------------------------------------
# Re-enrichment + the run
# --------------------------------------------------------------------------

def reenrich(conn: sqlite3.Connection, enrich: Enrich, limit: int = 10) -> list[dict]:
    """Look for the own site of each cleared entity still queued. One the
    lookup couldn't finish (budget, Firecrawl down) stays queued."""
    out = []
    for led in dict_rows(conn, "SELECT * FROM listing_website_clears WHERE reenrich = 'queued' "
                               "AND entity_uid IS NOT NULL ORDER BY updated_at, target LIMIT ?",
                         (limit,)):
        note = None
        try:
            status = enrich(conn, led["kind"], led["entity_uid"])
        except Exception as e:  # noqa: BLE001 — stays queued
            status, note = "deferred", f"enrichment failed: {str(e)[:200]}"
        if status != "deferred":
            conn.execute("UPDATE listing_website_clears SET reenrich = ?, updated_at = ? "
                         "WHERE target = ? AND reenrich = 'queued'",
                         (status, iso_utc_now(), led["target"]))
        conn.commit()
        website = conn.execute(f"SELECT website FROM {KINDS[led['kind']]} WHERE entity_uid = ?",
                               (led["entity_uid"],)).fetchone()
        out.append({"target": led["target"], "reenrich": status,
                    "website": website[0] if website else None, **({"note": note} if note else {})})
    return out


def enricher(llm_calls: int = 2) -> Enrich:
    """entity_enrich.enrich_entity under one shared budget, as
    event-enrich-entities: Budget.from_env() Firecrawl lookups, `llm_calls`
    Codex calls within the next 100s."""
    import time
    from engine import entity_classify, entity_enrich
    from engine.derive_helpers import town_name
    budget = entity_enrich.Budget.from_env()
    llm = entity_classify.codex_llm(llm_calls, 30, time.monotonic() + 100)
    town = town_name()
    return lambda conn, kind, uid: entity_enrich.enrich_entity(
        conn, kind, uid, town, budget, llm).status


def run(conn: sqlite3.Connection, client, *, enrich: Optional[Enrich] = None,
        limit: int = 20) -> dict:
    """The operator-triggered pass (`listing-website-cleanup apply`).
    Supervised: clear what the operator confirmed. Autonomous: clear local,
    flag live. Then re-enrich. In supervised mode with nothing confirmed or
    queued, no API call is made."""
    from engine import autonomy
    out: dict = {"mode": "autonomous" if autonomy.is_autonomous() else "supervised"}
    if out["mode"] == "autonomous":
        out.update(run_autonomous(conn, client, limit=limit))
    else:
        out["applied"] = apply_confirmed(conn, client, limit=limit)
    if enrich is not None:
        out["reenriched"] = reenrich(conn, enrich, limit=limit)
    return out
