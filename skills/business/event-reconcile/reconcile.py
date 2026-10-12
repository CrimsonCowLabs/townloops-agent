#!/usr/bin/env python3
"""event-reconcile — weekly: re-pull every approved seed, cross-reference with
the live TownLoops catalog, stage new/changed events locally, then enrich them
(event image, venue, host business/community + images).

Never writes to TownLoops. Everything lands in the local DB for the operator;
the Series (`series_slug`/`series_id`) each live event reports is mirrored onto
the local rows shipped as it;
approved rows flow through the normal derive → enrich → ship workqueue, and
event-ship PATCHes the live row for events staged as `changed`.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml
from dateutil import tz as _tz

from engine import entity_classify, entity_enrich, event_people, inspection, series_sync
from engine import lock as _lock
from engine import reconcile as rc
from engine.derive_helpers import (
    iso_utc_now, open_db, parse_extra_json, record_activity, seed_ref,
)
from engine.entity_enrich import Budget
from engine.event_extract import (
    build_extraction_prompt, codex_exec, event_uid, firecrawl_markdown,
    parse_json_array, town_tz_name, upsert_event,
)


_WORKER = "event-reconcile"
_HERMES_HOME = os.environ.get("HERMES_HOME", "/home/agent/.hermes")
_PROFILE_PATH = Path(os.environ.get(
    "TOWN_PROFILE_PATH", os.path.join(_HERMES_HOME, "town-profile.yaml"),
))
_REPORT_DIR = Path(_HERMES_HOME) / "state" / "reports"

# Extra keys asked of the LLM on top of event-find's base schema — the host
# organization feeds business/community derivation, image_url the event image.
EXTRA_FIELDS = (
    '  "organizer":   name of the business or organization hosting/presenting the event, if stated, else null\n'
    '  "organizer_kind": "business" (company, bar, restaurant, shop, resort) or "community" '
    '(nonprofit, club, church, school, civic body, group), or null\n'
    '  "image_url":   absolute URL of an image for this specific event, if the page shows one, else null\n'
)

# Codex reads of a new community's or venue's site for its meeting info or
# accessibility notes, per run. The run is detached from Hermes' cron
# timeout, so there is no deadline; a skipped or throttled read leaves the
# shell `pending` for event-enrich-entities.
_MAX_PAGE_READS = 10
_PAGE_READ_TIMEOUT_S = 40

_LIVE_RESOURCES = {"venue": "venues", "business": "businesses", "community": "communities"}


def _load_profile() -> dict:
    try:
        return yaml.safe_load(_PROFILE_PATH.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _town_tz():
    return _tz.gettz(town_tz_name())


def _merge_extra(raw: Optional[str], key: str, value: dict) -> str:
    try:
        extra = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        extra = {"_unparsed": raw}
    if not isinstance(extra, dict):
        extra = {"_value": extra}
    extra[key] = value
    return json.dumps(extra, ensure_ascii=False)


def _telegram(text: str) -> None:
    try:
        from engine.orchestrator import _telegram_notify
    except Exception:  # noqa: BLE001 — notification is best-effort
        return
    _telegram_notify(text)


# --------------------------------------------------------------------------
# Live catalog
# --------------------------------------------------------------------------

class Live:
    """Snapshot of the live TownLoops catalog for this run (read-only)."""

    def __init__(self, horizon_days: int):
        from engine.townloops_api import Client
        self.client = Client()
        self.client.verify_town()
        now = datetime.now(timezone.utc)
        params = {
            "from": (now - timedelta(days=2)).isoformat(timespec="seconds"),
            "to": (now + timedelta(days=horizon_days)).isoformat(timespec="seconds"),
        }
        self.events = list(self.client.paginate("events", **params))
        self.entities = {k: list(self.client.paginate(r)) for k, r in _LIVE_RESOURCES.items()}
        self.index = rc.LiveIndex.build(self.events, self.entities["venue"], _town_tz())


# --------------------------------------------------------------------------
# Phase 1 — pull + cross-reference + stage locally
# --------------------------------------------------------------------------

def _seeds(conn, seed_id: Optional[int], limit: Optional[int]) -> list[dict]:
    q = "SELECT * FROM event_seeds WHERE status = 'approved'"
    params: list = []
    if seed_id is not None:
        q += " AND seed_id = ?"
        params.append(seed_id)
    q += " ORDER BY seed_id"
    if limit:
        q += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def _reopen_for_review(conn, uid: str, venue_changed: bool) -> bool:
    """A source change to an event this agent already shipped (or gave up on)
    goes back to the operator queue; on approval it re-runs the pipeline and
    event-ship PATCHes the live row. A venue change drops the old venue so it
    is derived afresh. Declined rows stay declined. Returns True if reopened."""
    cur = conn.execute(
        "UPDATE events SET state = 'metadata-extracted', approval_status = 'pending', "
        "  state_updated_at = ?, "
        "  derived_venue_uid = CASE WHEN ? THEN NULL ELSE derived_venue_uid END "
        "WHERE event_uid = ? AND (state = 'shipped' OR state LIKE 'stuck-%')",
        (iso_utc_now(), 1 if venue_changed else 0, uid),
    )
    if not cur.rowcount:
        return False
    # Old queue rows keep their attempt counts (a stuck row is at the max),
    # which would stop re-enqueued actions from ever being claimed.
    conn.execute("DELETE FROM event_workqueue WHERE event_uid = ?", (uid,))
    if venue_changed:
        conn.execute("DELETE FROM linked_events WHERE event_uid = ? AND entity_kind = 'venue'", (uid,))
    return True


def _stage(conn, seed_name: str, c: rc.Classification, run_id: str) -> tuple[str, str]:
    """Upsert one new/changed extraction locally. Returns (event_uid, upsert action).

    `upsert_event` sets the UTC times from the extraction. The live slug of a
    changed event lives only in the reconcile metadata —
    `townloops_slug` is set by event-ship once the PATCH lands, so a row later
    re-staged as `new` can't inherit a slug that makes its create 409."""
    ev = c.extracted
    action = upsert_event(conn, seed_name, ev)
    uid = event_uid(seed_name, ev.get("title") or "", ev.get("date") or "")
    row = conn.execute(
        "SELECT extra_json FROM events WHERE event_uid = ?", (uid,),
    ).fetchone()
    live = c.live or {}
    meta = {
        "run": run_id,
        "kind": c.kind,
        "matched_by": c.matched_by,
        "reason": c.reason,
        "changes": c.changes,
        "live_slug": live.get("slug"),
        "live_id": live.get("id"),
        "live_venue_slug": live.get("venue_slug"),
        "live_start_at": live.get("start_at"),
        "live_end_at": live.get("end_at"),
        "organizer": (ev.get("organizer") or "").strip() or None,
        "organizer_kind": ev.get("organizer_kind") if ev.get("organizer_kind") in ("business", "community") else None,
    }
    # Reopen only when the source itself changed since we last saw it; if only
    # the live row differs (edited on TownLoops), leave the human edit alone.
    if c.kind == "changed" and action != "unchanged":
        _reopen_for_review(conn, uid, venue_changed="venue" in c.changes)
    note = ("[reconcile] new — not on TownLoops" if c.kind == "new"
            else f"[reconcile] live event {live.get('slug')} changed: {c.summary()}")
    conn.execute(
        "UPDATE events SET extra_json = ?, "
        "  image_url = COALESCE(image_url, ?), "
        "  townloops_slug = CASE WHEN ? AND pushed_at IS NULL THEN NULL ELSE townloops_slug END, "
        "  approval_note = CASE WHEN approval_status = 'pending' THEN ? ELSE approval_note END "
        "WHERE event_uid = ?",
        (_merge_extra(row["extra_json"], "reconcile", meta),
         ev.get("image_url") if isinstance(ev.get("image_url"), str) and ev["image_url"].startswith("http") else None,
         1 if c.kind == "new" else 0,
         note, uid),
    )
    return uid, action


def _annotate_already_live(conn, seed_name: str, c: rc.Classification) -> bool:
    """If event-find already queued this (unchanged, live) event, flag it so
    the operator doesn't approve a duplicate. Returns True if a row was flagged."""
    ev = c.extracted
    uid = event_uid(seed_name, ev.get("title") or "", ev.get("date") or "")
    cur = conn.execute(
        "UPDATE events SET approval_note = ? "
        "WHERE event_uid = ? AND approval_status = 'pending'",
        (f"[reconcile] already live as {c.live.get('slug')} — likely duplicate", uid),
    )
    return cur.rowcount > 0


def pull(conn, live: Live, profile: dict, args, run_id: str) -> dict:
    stats = {"seeds": 0, "seed_errors": 0, "extracted": 0, "unchanged": 0,
             "new": 0, "changed": 0, "skipped": 0, "flagged_duplicates": 0,
             "staged_uids": [], "changes": []}
    for seed in _seeds(conn, args.seed_id, args.limit):
        stats["seeds"] += 1
        name, url = seed["name"], seed["url"]
        started = iso_utc_now()
        inspection.record(worker=_WORKER, action="firecrawl", target=url,
                          detail=f"weekly pull {name}")
        ok, md, err = firecrawl_markdown(url)
        if ok:
            prompt = build_extraction_prompt(profile, name, url, md, extra_fields=EXTRA_FIELDS)
            ok, out, err = codex_exec(prompt, timeout_s=180)
        if not ok:
            stats["seed_errors"] += 1
            conn.execute(
                "INSERT INTO source_runs (source, started_at, ended_at, ok, fetched, inserted, updated, error) "
                "VALUES (?, ?, ?, 0, 0, 0, 0, ?)",
                (seed_ref(name), started, iso_utc_now(), f"reconcile: {err}"[:400]),
            )
            print(json.dumps({"seed": name, "error": err[:200]}))
            continue

        events = [e for e in parse_json_array(out)
                  if isinstance(e, dict) and e.get("title") and e.get("date")]
        stats["extracted"] += len(events)
        per = {"seed": name, "extracted": len(events), "new": 0, "changed": 0, "unchanged": 0}
        for c in rc.classify_all(events, live.index, seed_url=url):
            stats[c.kind] += 1
            if c.kind in per:
                per[c.kind] += 1
            if c.kind == "unchanged":
                if not args.dry_run and _annotate_already_live(conn, name, c):
                    stats["flagged_duplicates"] += 1
                continue
            if c.kind == "skipped":
                continue
            if c.kind == "changed":
                stats["changes"].append({
                    "title": c.extracted.get("title"), "live_slug": c.live.get("slug"),
                    "summary": c.summary(),
                })
            if args.dry_run:
                print(json.dumps({"seed": name, "kind": c.kind, "title": c.extracted.get("title"),
                                  "date": c.extracted.get("date"), "summary": c.summary(),
                                  "reason": c.reason}, ensure_ascii=False))
                continue
            uid, _ = _stage(conn, name, c, run_id)
            stats["staged_uids"].append(uid)
        if not args.dry_run:
            conn.execute(
                "INSERT INTO source_runs (source, started_at, ended_at, ok, fetched, inserted, updated, error) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, NULL)",
                (seed_ref(name), started, iso_utc_now(), len(events), per["new"], per["changed"]),
            )
        print(json.dumps(per))
    return stats


# --------------------------------------------------------------------------
# Phase 2 — enrichment (event image, venue, host + images)
# --------------------------------------------------------------------------

def _resolve_and_tally(conn, kind: str, name: str, event: dict, live: Live,
                   town: str, budget: Budget, report: dict,
                   llm: Optional[entity_classify.LLM] = None) -> str:
    """Resolve via engine.entity_enrich and tally the outcome into `report`."""
    r = entity_enrich.resolve_entity(conn, kind, name, event, live.entities[kind], town, budget,
                                     llm)
    if r.image_proposal:
        # A live mirror's image proposal, whether just mirrored or matched locally.
        report["live_image_proposals"].append(r.image_proposal)
    if r.outcome == "live":
        report["live_matched"][kind] += 1
    elif r.outcome == "created":
        report["created"][kind].append({"name": name, "filled": r.filled, "status": r.status})
    return r.entity_uid


def enrich_event(conn, event: dict, live: Live, town: str, budget: Budget, report: dict,
                 llm: Optional[entity_classify.LLM] = None) -> None:
    from engine.research import research_url, verify_image
    uid = event["event_uid"]
    meta = parse_extra_json(event.get("extra_json")).get("reconcile") or {}

    # Event image (+ description backfill) from the event's detail page.
    if event.get("image_url") and not verify_image(event["image_url"]):
        conn.execute("UPDATE events SET image_url = NULL WHERE event_uid = ?", (uid,))
        event["image_url"] = None
    if (not event.get("image_url") or not event.get("description")) and event.get("source_url") \
            and budget.take():
        res = research_url(event["source_url"], use_firecrawl=True)
        conn.execute(
            "UPDATE events SET image_url = COALESCE(image_url, ?), "
            "  description = CASE WHEN COALESCE(description, '') = '' THEN ? ELSE description END "
            "WHERE event_uid = ?",
            (res.payload.get("image_path"), res.payload.get("description"), uid),
        )
    if conn.execute("SELECT image_url FROM events WHERE event_uid = ?", (uid,)).fetchone()[0]:
        report["event_images"] += 1

    # Venue.
    venue = (event.get("venue_raw") or event.get("venue_name") or "").strip()
    if venue and not event.get("derived_venue_uid"):
        vuid = _resolve_and_tally(conn, "venue", venue, event, live, town, budget, report, llm)
        conn.execute("UPDATE events SET derived_venue_uid = ? WHERE event_uid = ? AND derived_venue_uid IS NULL",
                     (vuid, uid))

    # Host business / community — never a venue (the venue link carries it).
    org, org_kind = meta.get("organizer"), meta.get("organizer_kind")
    if org and org_kind in ("business", "community") and not event.get("derived_host_uid"):
        bound = conn.execute("SELECT derived_venue_uid FROM events WHERE event_uid = ?",
                             (uid,)).fetchone()[0]
        is_venue = event_people.host_venue(conn, org, {**event, "derived_venue_uid": bound},
                                           lambda: live.entities["venue"])
        if is_venue:
            record_activity(conn, _WORKER, "host-is-venue", uid,
                            detail=f"{org_kind} '{org}' is {is_venue} — no host")
        else:
            huid = _resolve_and_tally(conn, org_kind, org, event, live, town, budget, report, llm)
            conn.execute(
                "UPDATE events SET derived_host_uid = ?, derived_host_kind = ? "
                "WHERE event_uid = ? AND derived_host_uid IS NULL",
                (huid, org_kind, uid),
            )
    record_activity(conn, _WORKER, "enriched", uid, detail=f"venue={venue[:40]} host={org or '-'}")


def enrich(conn, live: Live, profile: dict, uids: Optional[list[str]], max_lookups: int,
           llm: Optional[entity_classify.LLM] = None) -> dict:
    town = profile.get("name") or os.environ.get("TOWNLOOPS_TOWN_SLUG", "")
    report = {"events": 0, "event_images": 0, "live_matched": {k: 0 for k in entity_enrich.TABLES},
              "created": {k: [] for k in entity_enrich.TABLES}, "live_image_proposals": []}
    if uids is None:
        rows = conn.execute(
            "SELECT * FROM events WHERE approval_status = 'pending' "
            "AND json_extract(extra_json, '$.reconcile.kind') IN ('new', 'changed')"
        ).fetchall()
    else:
        marks = ",".join("?" for _ in uids) or "''"
        rows = conn.execute(f"SELECT * FROM events WHERE event_uid IN ({marks})", uids).fetchall()
    budget = Budget(max_lookups)
    for r in rows:
        enrich_event(conn, dict(r), live, town, budget, report, llm)
        report["events"] += 1
    report["lookups_left"] = budget.left
    return report


# --------------------------------------------------------------------------
# Report + CLI
# --------------------------------------------------------------------------

def _write_report(run_id: str, pulled: Optional[dict], enriched: Optional[dict]) -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = _REPORT_DIR / f"reconcile-{run_id[:10]}.md"
    town = os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")
    lines = [f"# {town} — weekly reconcile {run_id}", ""]
    if pulled:
        lines += [
            f"- Seeds pulled: {pulled['seeds']} ({pulled['seed_errors']} errors), events extracted: {pulled['extracted']}",
            f"- Already live & unchanged: {pulled['unchanged']} (flagged {pulled['flagged_duplicates']} pending duplicates)",
            f"- **New (not on TownLoops): {pulled['new']}**",
            f"- **Changed on source: {pulled['changed']}**",
            "",
        ]
        for c in pulled["changes"]:
            lines.append(f"  - `{c['live_slug']}` {c['title']}: {c['summary']}")
    if enriched:
        lines += ["", "## Enrichment",
                  f"- Events enriched: {enriched['events']}, with an image: {enriched['event_images']}",
                  f"- Matched to live entities: {enriched['live_matched']}"]
        for kind, items in enriched["created"].items():
            for it in items:
                if it.get("status") == "pending":
                    lines.append(f"- New {kind}: {it['name']} (research deferred to next run — lookup budget ran out or site unreadable)")
                else:
                    lines.append(f"- New {kind}: {it['name']} (filled: {', '.join(it['filled']) or 'nothing found'})")
        for p in enriched["live_image_proposals"]:
            lines.append(f"- Image proposed for live {p['kind']} `{p['slug']}`: {p['image_path']}")
    path.write_text("\n".join(lines) + "\n")
    return path


def cmd_run(args) -> int:
    from engine import db as _db
    mconn = _db.connect()
    _db.run_migrations(mconn)  # 016 adds communities.enrichment_status
    mconn.close()
    conn = open_db()
    profile = _load_profile()
    run_id = iso_utc_now()
    try:
        with _lock.acquire("event-reconcile"):
            live = Live(args.horizon_days)
            print(json.dumps({"live_events": len(live.events),
                              **{f"live_{k}": len(v) for k, v in live.entities.items()}}))
            if not getattr(args, "dry_run", False):
                series_sync.mirror(conn, live.events)
            pulled = None if args.cmd == "enrich" else pull(conn, live, profile, args, run_id)
            enriched = None
            llm = entity_classify.codex_llm(_MAX_PAGE_READS, _PAGE_READ_TIMEOUT_S, float("inf"))
            if args.cmd == "enrich":
                enriched = enrich(conn, live, profile, None, args.max_lookups, llm)
            elif not args.dry_run and not args.skip_enrich:
                enriched = enrich(conn, live, profile, pulled["staged_uids"], args.max_lookups, llm)
    except _lock.LockHeld as e:
        # Not a failure — a slow run is still going; the supervisor in
        # scripts/event_reconcile.py would report a nonzero exit as one.
        print(json.dumps({"skipped": f"previous run still active: {e}"}))
        inspection.record(worker=_WORKER, action="skipped-overlap", target=args.cmd,
                          detail=f"previous run still active ({e.holder})")
        return 0

    summary = {"run": run_id, "dry_run": getattr(args, "dry_run", False)}
    if pulled:
        summary.update({k: pulled[k] for k in ("seeds", "seed_errors", "extracted", "unchanged",
                                               "new", "changed", "skipped", "flagged_duplicates")})
    if enriched:
        summary["enrich"] = {"events": enriched["events"], "event_images": enriched["event_images"],
                             "created": {k: len(v) for k, v in enriched["created"].items()},
                             "live_matched": enriched["live_matched"],
                             "live_image_proposals": len(enriched["live_image_proposals"])}
    if not summary["dry_run"]:
        path = _write_report(run_id, pulled, enriched)
        summary["report"] = str(path)
        if pulled:
            _telegram(f"[{os.environ.get('TOWNLOOPS_TOWN_SLUG', '?')}] weekly reconcile: "
                      f"{pulled['new']} new, {pulled['changed']} changed, {pulled['unchanged']} already live "
                      f"→ pending review."
                      + (f" {pulled['seed_errors']} of {pulled['seeds']} seeds failed to pull."
                         if pulled["seed_errors"] else "")
                      + f" Report: {path.name}")
    inspection.record(worker=_WORKER, action="skill-invoke", target=args.cmd,
                      detail=json.dumps(summary)[:200])
    print(json.dumps({"summary": summary}, ensure_ascii=False))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="event-reconcile")
    sub = p.add_subparsers(dest="cmd")
    for name, help_ in (("run", "pull + cross-reference + stage + enrich (default)"),
                        ("enrich", "enrich all pending reconcile-staged events only")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--horizon-days", type=int, default=180,
                        help="live catalog window to cross-reference (default 180)")
        sp.add_argument("--max-lookups", type=int, default=80,
                        help="cap on Firecrawl search/scrape calls during enrichment")
        if name == "run":
            sp.add_argument("--seed-id", type=int)
            sp.add_argument("--limit", type=int)
            sp.add_argument("--dry-run", action="store_true",
                            help="classify and print; write nothing locally")
            sp.add_argument("--skip-enrich", action="store_true")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("run", "enrich", "-h", "--help"):
        argv.insert(0, "run")
    return cmd_run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
