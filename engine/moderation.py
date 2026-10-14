"""Moderation feedback: what the TownLoops moderator did with each record the
agent submitted in autonomous mode (engine/autonomy.GatedClient records them
in `submissions`).

    pending ──GET──▶ published | rejected            (the record's `status`)
       └──404──▶ merged  (into the published record it was folded into —
                          the local row is re-pointed there)
              └─▶ gone   (deleted, or merged somewhere we can't find: listed
                          in the digest)

A merge and a delete both 404. A change submission counts as merged when
its target now shows any value it carried that the target didn't (recorded
by event-ship on the event as `change_submission`); a cancellation, when the
target shows the cancelled marking.

An entity GET with no `status` field (TownLoops before the entity gate) is
published; a merged entity's 404 names its survivor (`merged_into_slug`). Outcomes are attributed to the event's seed and Series (and an
entity's discovery source): seed trust is refreshed, a high rejection rate
opens the circuit (engine/autonomy.check_rejection_rate), and a rejected
discovered organisation is remembered by name so it is never proposed again.

`digest()` is the nightly digest's autonomous section (including the
listing-page websites still live that listing-website-cleanup flagged), and `digest_markdown`
/ `digest_telegram` format it (engine/orchestrator.summary sends them).
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Optional

from engine import autonomy
from engine.derive_helpers import (
    dict_rows as _rows, iso_utc_now, names_match, parse_extra_json, parse_instant,
    seed_name_of,
)


ENTITY_TABLES = {"venues": "venues", "businesses": "businesses", "communities": "communities",
                 "performers": "performers", "creators": "creators"}
_WINDOW = timedelta(hours=12)


def poll(client, conn: sqlite3.Connection, s: autonomy.Settings, *, limit: int = 100) -> dict:
    """Check up to `limit` pending submissions, least recently checked first."""
    from engine.live_lookup import is_not_found
    out = {"checked": 0, "published": 0, "rejected": 0, "merged": 0, "gone": 0, "pending": 0,
           "errors": []}
    seeds: set[str] = set()
    for sub in _rows(conn, "SELECT * FROM submissions WHERE status = 'pending' "
                           "ORDER BY checked_at IS NOT NULL, checked_at, id LIMIT ?", (limit,)):
        out["checked"] += 1
        try:
            live = client.get(sub["resource"], sub["slug"]) or {}
        except Exception as e:  # noqa: BLE001
            if not is_not_found(e):
                out["errors"].append(f"{sub['resource']}/{sub['slug']}: {str(e)[:200]}")
                continue
            outcome, survivor = _after_404(client, conn, sub, e)
        else:
            outcome = live.get("status") or ("published" if sub["resource"] != "events" else None)
            survivor = None
        if outcome not in ("published", "rejected", "merged", "gone"):
            conn.execute("UPDATE submissions SET checked_at = ? WHERE id = ?",
                         (iso_utc_now(), sub["id"]))
            out["pending"] += 1
            continue
        _decide(conn, sub, outcome, survivor)
        out[outcome] += 1
        if sub.get("seed"):
            seeds.add(sub["seed"])
    conn.commit()
    if seeds:
        autonomy.refresh_trust(conn, seeds)
    breaker = autonomy.check_rejection_rate(conn, s)
    conn.commit()
    if breaker:
        out["circuit_opened"] = breaker
    return out


def _decide(conn: sqlite3.Connection, sub: dict, outcome: str, survivor: Optional[str]) -> None:
    now = iso_utc_now()
    conn.execute("UPDATE submissions SET status = ?, decided_at = ?, checked_at = ?, "
                 "  surviving_slug = ? WHERE id = ?", (outcome, now, now, survivor, sub["id"]))
    evidence = {k: sub[k] for k in ("resource", "slug", "kind", "seed", "series_slug",
                                    "discovery_source", "target_slug") if sub.get(k)}
    if survivor:
        evidence["surviving_slug"] = survivor
    autonomy.record(conn, "submission", sub["local_uid"] or sub["slug"],
                    autonomy.Decision(outcome, f"moderator-{outcome}", evidence))
    if sub["resource"] == "events":
        if outcome == "merged" and survivor:
            _repoint_event(conn, sub, survivor)
        return
    table = ENTITY_TABLES.get(sub["resource"])
    if table and outcome == "merged" and survivor:
        conn.execute(f"UPDATE {table} SET townloops_slug = ? WHERE townloops_slug = ?",
                     (survivor, sub["slug"]))
    if table and outcome == "rejected":
        _remember_rejection(conn, table, sub)


def _repoint_event(conn: sqlite3.Connection, sub: dict, survivor: str) -> None:
    """The local event now lives as `survivor`. A change folded into the
    published event also retires the old-date row that still mapped there."""
    uid = sub["local_uid"]
    if not uid:
        return
    conn.execute("UPDATE events SET townloops_slug = ? WHERE event_uid = ?", (survivor, uid))
    if sub.get("kind") in ("change", "cancellation"):
        conn.execute(
            "UPDATE events SET status = 'superseded', townloops_slug = NULL WHERE "
            "townloops_slug = ? AND event_uid != ? AND status != 'superseded'", (survivor, uid))


def _after_404(client, conn: sqlite3.Connection, sub: dict,
               err: Exception) -> tuple[str, Optional[str]]:
    """A submission gone from TownLoops: merged into the record we can find
    it folded into, else gone. A merged entity's 404 names its survivor
    (`merged_into_slug`, townloops ADR 0009); without one, it's looked up."""
    if sub["resource"] == "events":
        if sub.get("target_slug"):
            # Both a merge and a delete 404; only a target that now shows
            # what we sent was merged into.
            return ("merged", sub["target_slug"]) if _change_landed(client, conn, sub) \
                else ("gone", None)
        survivor = _event_survivor(client, conn, sub)
    else:
        from engine.live_lookup import merged_into_slug
        survivor = merged_into_slug(err) or _entity_survivor(client, conn, sub)
    return ("merged", survivor) if survivor else ("gone", None)


def _change_landed(client, conn: sqlite3.Connection, sub: dict) -> bool:
    from engine.live_lookup import find_live_entity
    try:
        target = find_live_entity(client, "events", sub["target_slug"]) or {}
    except Exception:  # noqa: BLE001 — unanswered: not decided as merged
        return False
    if sub.get("kind") == "cancellation":
        return (target.get("title") or "").lower().startswith("cancelled") \
            or "cancelled" in (target.get("tags") or [])
    sent = _change_fields(conn, sub)
    if sent:
        # Merged if the target now shows any of the changed values — a
        # moderator may merge only some of them.
        from engine.live_patch import shows
        return any(shows(target, f, v) for f, v in sent.items())
    row = conn.execute("SELECT date_utc FROM events WHERE event_uid = ?",
                       (sub.get("local_uid"),)).fetchone()
    mine, theirs = parse_instant(row[0]) if row else None, parse_instant(target.get("start_at"))
    return bool(mine and theirs and mine == theirs)


def _change_fields(conn: sqlite3.Connection, sub: dict) -> dict:
    """The values a change submission carried that its target didn't show
    (event-ship records them on the event); {} for one recorded before that."""
    row = conn.execute("SELECT extra_json FROM events WHERE event_uid = ?",
                       (sub.get("local_uid"),)).fetchone()
    rec = parse_extra_json(row[0] if row else None).get("change_submission") or {}
    return (rec.get("fields") or {}) if rec.get("pending_slug") == sub["slug"] else {}


def _event_survivor(client, conn: sqlite3.Connection, sub: dict) -> Optional[str]:
    from engine.live_lookup import events_between
    row = conn.execute("SELECT title, name, date_utc, source_url FROM events WHERE event_uid = ?",
                       (sub["local_uid"],)).fetchone() if sub.get("local_uid") else None
    start = parse_instant(row[2]) if row else None
    if not start:
        return None
    title, url = row[0] or row[1], (row[3] or "").rstrip("/")
    hits = [r for r in events_between(client, start - _WINDOW, start + _WINDOW, status="published")
            if r.get("slug") != sub["slug"] and (
                (url and (r.get("source_url") or "").rstrip("/") == url)
                or names_match(title, r.get("title")))]
    return hits[0]["slug"] if len(hits) == 1 else None


def _entity_survivor(client, conn: sqlite3.Connection, sub: dict) -> Optional[str]:
    table = ENTITY_TABLES.get(sub["resource"])
    row = conn.execute(f"SELECT name FROM {table} WHERE townloops_slug = ?",
                       (sub["slug"],)).fetchone() if table else None
    if not row or not row[0]:
        return None
    hits = [r for r in client.paginate(sub["resource"])
            if r.get("slug") != sub["slug"] and r.get("status", "published") == "published"
            and names_match(row[0], r.get("name"))]
    return hits[0]["slug"] if len(hits) == 1 else None


def _remember_rejection(conn: sqlite3.Connection, table: str, sub: dict) -> None:
    """A rejected discovered organisation: its candidate is rejected with a
    `townloops` reason, which discovery matches by name from then on."""
    from engine import business_discovery as bd
    row = conn.execute(f"SELECT name, extra_json FROM {table} WHERE townloops_slug = ?",
                       (sub["slug"],)).fetchone()
    if not row:
        return
    cand = parse_extra_json(row[1]).get("candidate_uid")
    reason = f"{bd.TOWNLOOPS_REASON}: moderator rejected {sub['slug']}"
    if cand:
        conn.execute("UPDATE discovery_candidates SET status = 'rejected', reject_reason = ? "
                     "WHERE candidate_uid = ?", (reason, cand))


def outcomes_since(conn: sqlite3.Connection, since: str) -> dict:
    """Digest numbers: what was submitted and decided since `since`."""
    submitted = dict(conn.execute(
        "SELECT resource, COUNT(*) FROM submissions WHERE submitted_at >= ? GROUP BY resource",
        (since,)).fetchall())
    decided = dict(conn.execute(
        "SELECT status, COUNT(*) FROM submissions WHERE decided_at >= ? AND status != 'pending' "
        "GROUP BY status", (since,)).fetchall())
    return {"submitted": submitted, "decided": decided,
            "pending": autonomy.pending_count(conn)}


def digest(conn: sqlite3.Connection, since: str, series_report: Optional[dict] = None) -> dict:
    """The nightly digest's autonomous section, from the decision log and
    `submissions`: what was submitted, dropped (top reasons), decided by the
    moderator, done to seeds and organisations, and what needs the operator."""
    from collections import Counter
    log = autonomy.decisions(conn, since=since, limit=100_000)
    by = Counter((d["subject"], d["verdict"]) for d in log)
    drops = Counter(d["reason"] for d in log if d["subject"] == "event" and d["verdict"] == "drop")
    o = outcomes_since(conn, since)
    decided = o["decided"]
    accepted = decided.get("published", 0) + decided.get("merged", 0)
    n = accepted + decided.get("rejected", 0)
    seeds = [d for d in log if d["subject"] == "seed"]
    attention: list[str] = []
    c = autonomy.circuit(conn)
    if c:
        attention.append(f"submissions stopped: {c.get('reason')} (since {c.get('at')}) — "
                         "`autonomy resume` once fixed")
    for r in _rows(conn, "SELECT resource, slug FROM submissions WHERE status = 'gone' "
                         "AND decided_at >= ?", (since,)):
        attention.append(f"{r['resource']} {r['slug']} left the queue but no surviving "
                         "record was found — check whether it was merged or deleted")
    for r in _rows(conn, "SELECT COALESCE(title, name) AS title, date_local, townloops_slug, "
                         "cancel_note FROM events WHERE cancel_status = 'possibly-cancelled' "
                         "ORDER BY date_utc"):
        attention.append(f"possibly cancelled (not acted on): {r['title']} {r['date_local'] or ''}"
                         f" ({r['townloops_slug']}) — {r['cancel_note']}")
    for d in log:
        if d["subject"] == "seed" and d["verdict"] == "relocate":
            attention.append(f"seed {seed_name_of(d['target']) or d['target']} moved: "
                             f"{d['evidence'].get('from')} → {d['evidence'].get('to')}")
    stuck = conn.execute("SELECT COUNT(*) FROM events WHERE state LIKE 'stuck-%'").fetchone()[0]
    if stuck:
        attention.append(f"{stuck} event(s) stuck, waiting out a retry backoff")
    report = series_report or {}
    from engine.listing_website_cleanup import flagged
    return {
        "events_submitted": o["submitted"].get("events", 0),
        "entities_submitted": sum(v for k, v in o["submitted"].items() if k != "events"),
        "events_approved": by[("event", "approve")],
        "events_dropped": sum(drops.values()),
        "top_drop_reasons": drops.most_common(5),
        "moderation": {"published": decided.get("published", 0),
                       "merged": decided.get("merged", 0),
                       "rejected": decided.get("rejected", 0), "gone": decided.get("gone", 0),
                       "approval_rate": round(accepted / n, 2) if n else None,
                       "rejection_rate": round(decided.get("rejected", 0) / n, 2) if n else None},
        "pending_in_townloops": o["pending"],
        "seeds_added": [seed_name_of(d["target"]) or d["target"] for d in seeds
                        if d["verdict"] == "approve"],
        "seeds_retired": [f"{seed_name_of(d['target']) or d['target']} ({d['reason']})"
                          for d in seeds if d["verdict"] == "retire"],
        "organisations_approved": by[("candidate", "submit")],
        "series_candidates": report.get("clusters") or [],
        "suspect_series": (report.get("multi_venue") or []) + (report.get("duplicate_dates") or []),
        "needs_attention": attention,
        # Listing-page websites still live (listing-website-cleanup flags them;
        # the agent never PATCHes them) — listed until an apply sees live fixed.
        "listing_websites_live": [f"{r['name'] or r['target']} ({r['live_slug']}): {r['url']}"
                                  for r in flagged(conn)],
    }


def digest_markdown(a: dict) -> list[str]:
    """`digest()` as the nightly report's markdown section."""
    m = a["moderation"]
    rate = lambda v: "n/a" if v is None else f"{v:.0%}"  # noqa: E731
    lines = ["", "## Autonomous decisions (last 24h)",
             f"- Events submitted to TownLoops: {a['events_submitted']} "
             f"(entities: {a['entities_submitted']}); still pending in TownLoops: "
             f"{a['pending_in_townloops']}",
             f"- Events approved by the agent: {a['events_approved']}; dropped: "
             f"{a['events_dropped']}"
             + (" — " + ", ".join(f"{r} ×{n}" for r, n in a["top_drop_reasons"])
                if a["top_drop_reasons"] else ""),
             f"- Moderator: {m['published']} published, {m['merged']} merged, "
             f"{m['rejected']} rejected (approval {rate(m['approval_rate'])}, "
             f"rejection {rate(m['rejection_rate'])})",
             f"- Seeds added: {', '.join(a['seeds_added']) or 'none'}; retired: "
             f"{', '.join(a['seeds_retired']) or 'none'}",
             f"- Organisations approved for submission: {a['organisations_approved']}"]
    lines += ["", "## Series candidates"]
    lines += [f"- **{c.get('title')}** — {c.get('dates')} dates at "
              f"`{c.get('venue_slug') or c.get('venue_id')}`" for c in a["series_candidates"]] \
        or ["- (none in the latest weekly report)"]
    if a["suspect_series"]:
        lines += [f"- Suspect Series `{d.get('slug')}`: {d.get('venues')} venues"
                  + (f", same-day duplicates {', '.join(d.get('dates') or [])}"
                     if d.get("dates") else "") for d in a["suspect_series"]]
    lines += ["", "## Needs attention"] + ([f"- {x}" for x in a["needs_attention"]]
                                          or ["- nothing"])
    if a["listing_websites_live"]:
        lines += ["", "## Listing websites live",
                  "Live still holds a listing page as the website — fix in TownLoops admin, "
                  "then `listing-website-cleanup apply` clears the flag:"]
        lines += [f"- {x}" for x in a["listing_websites_live"]]
    return lines


def digest_telegram(a: dict) -> str:
    """`digest()` as the nightly Telegram summary's lines."""
    m = a["moderation"]
    out = (f"submitted: {a['events_submitted']} events, {a['entities_submitted']} entities "
           f"({a['pending_in_townloops']} pending in TownLoops)\n"
           f"dropped: {a['events_dropped']}"
           + (" — top: " + ", ".join(f"{r} ×{n}" for r, n in a["top_drop_reasons"][:3])
              if a["top_drop_reasons"] else "") + "\n"
           f"moderator: {m['published'] + m['merged']} accepted, {m['rejected']} rejected\n"
           f"seeds: +{len(a['seeds_added'])} added, {len(a['seeds_retired'])} retired\n")
    if a["series_candidates"]:
        out += f"Series candidates: {len(a['series_candidates'])}\n"
    if a["listing_websites_live"]:
        out += (f"listing websites live: {len(a['listing_websites_live'])} "
                "(fix in TownLoops admin)\n")
    if a["needs_attention"]:
        out += "needs attention:\n" + "\n".join(f"• {x}" for x in a["needs_attention"][:8]) + "\n"
    return out
