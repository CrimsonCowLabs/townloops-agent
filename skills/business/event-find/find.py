#!/usr/bin/env python3
"""event-find — scrape approved event_seeds, LLM-extract upcoming events, insert as pending.

In autonomous mode (engine/autonomy.py) each seed's pending events are then
judged against that seed's page — approved into the workqueue or declined
with a reason — instead of waiting for the operator. Pending events of a
seed that is no longer approved are judged without a page."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

import yaml

from engine import (autonomy, event_cancel, event_dedup, event_state, inspection, seed_adapters,
                    source_health)
from engine import lock as _lock
from engine.derive_helpers import parse_extra_json, seed_ref
from engine.event_extract import (  # noqa: F401 — re-exported for callers/tests
    build_extraction_prompt as _build_extraction_prompt,
    codex_exec as _codex_exec,
    content_hash as _content_hash,
    event_uid as _event_uid,
    extracted_events as _extracted_events,
    firecrawl_listing as _firecrawl_listing,
    normalize_title as _normalize_title,
    parse_json_array as _parse_json_array,
    upsert_event as _upsert_event,
)


_HERMES_HOME = os.environ.get("HERMES_HOME", "/home/agent/.hermes")
_DB_PATH = Path(os.environ.get(
    "TOWNLOOPS_DB_PATH",
    os.path.join(_HERMES_HOME, "state", "townloops.db"),
))
_PROFILE_PATH = Path(os.environ.get(
    "TOWN_PROFILE_PATH",
    os.path.join(_HERMES_HOME, "town-profile.yaml"),
))


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_town_profile() -> dict:
    if not _PROFILE_PATH.exists():
        return {}
    try:
        return yaml.safe_load(_PROFILE_PATH.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _seed_selection(conn: sqlite3.Connection, seed_id: Optional[int],
                    limit: Optional[int]) -> list[dict]:
    conn.row_factory = sqlite3.Row
    q = "SELECT * FROM event_seeds WHERE status = 'approved'"
    params: list = []
    if seed_id is not None:
        q += " AND seed_id = ?"
        params.append(seed_id)
    q += " ORDER BY last_scraped_at NULLS FIRST"
    if limit:
        q += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(q, params).fetchall()]


# Codex answered with an empty JSON array (as opposed to unparseable output).
_EMPTY_LIST_RE = re.compile(r"\[\s*\]\s*$")


def _telegram(text: str) -> None:
    try:
        from engine.orchestrator import _telegram_notify
    except Exception:  # noqa: BLE001 — alerts are best-effort
        return
    _telegram_notify(text)


def _pending_rows(conn: sqlite3.Connection, where: str, args: tuple) -> list[dict]:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(
        "SELECT * FROM events WHERE state = 'metadata-extracted' AND approval_status = 'pending' "
        f"AND status = 'active' AND {where} ORDER BY date_utc, event_uid", args).fetchall()]


def _series_checks(conn: sqlite3.Connection, s: autonomy.Settings):
    """(series_duplicate, series_blocked, series_established) for the
    judgement: an event whose title predicts an active Series is a duplicate
    when that Series already has an occurrence on its town-local date,
    skipped while the moderator keeps rejecting the Series, and approved
    without the AI pass while the moderator keeps publishing it. Without a
    TownLoops client (or with no Series) only the streak checks run, on
    what was recorded."""
    from engine import series
    from engine.event_extract import town_tz_name
    from engine.derive_helpers import parse_instant
    from engine.live_lookup import events_between
    from dateutil import tz as _tz
    from datetime import datetime as _dt, time as _time, timedelta as _td
    client, snapshot = None, []
    if os.environ.get("TOWNLOOPS_API_KEY") and os.environ.get("TOWNLOOPS_API_URL"):
        try:
            from engine.townloops_api import Client
            client = Client()
            snapshot = series.load_snapshot(client)
        except Exception as e:  # noqa: BLE001 — ship's live lookup still catches it
            print(json.dumps({"series_snapshot_error": str(e)[:200]}))
            client, snapshot = None, series.load_snapshot()
    by_key = {series.series_key(x.get("title")): x["slug"] for x in snapshot if x.get("slug")}
    zone = _tz.gettz(town_tz_name())

    def predicted(item: dict):
        return by_key.get(series.series_key(item.get("title")))

    def duplicate(item: dict):
        slug = predicted(item)
        start = parse_instant(item.get("date_utc"))
        if not (client and slug and start) or item.get("live_slug"):
            return None
        lo = _dt.combine(start.astimezone(zone).date(), _time.min, tzinfo=zone)
        for row in events_between(client, lo, lo + _td(days=1) - _td(seconds=1), series=slug):
            return row.get("slug") or "?"
        return None

    def blocked(item: dict):
        slug = predicted(item)
        return slug if autonomy.series_blocked(conn, slug, s) else None

    def established(item: dict):
        slug = predicted(item)
        streak = autonomy.series_established(conn, slug, s)
        return (slug, streak) if streak else None

    return duplicate, blocked, established


def _apply_judgement(conn: sqlite3.Connection, rows: list[dict], decisions: list) -> dict:
    counts = {"approved": 0, "dropped": 0, "deferred": 0}
    for row, d in zip(rows, decisions):
        uid = row["event_uid"]
        if d is None:
            autonomy.record(conn, "event", uid, autonomy.Decision(
                "defer", "ai-unavailable", {"note": "judged again next run"}))
            counts["deferred"] += 1
        elif d.verdict == "approve":
            if event_state.approve(conn, uid, f"[autonomy] {d.reason}"):
                filled = event_dedup.best_of_copies(conn, row)
                if filled:
                    d.evidence["filled_from_copies"] = filled
                autonomy.record(conn, "event", uid, d)
                counts["approved"] += 1
        elif event_state.decline(conn, uid, f"[autonomy] {d.reason}"):
            autonomy.record(conn, "event", uid, d)
            counts["dropped"] += 1
    conn.commit()
    return counts


def _judge(conn, rows: list[dict], seen: set[str], page, ctx, llm) -> dict:
    items = [autonomy.event_item(r, on_page=r["event_uid"] in seen) for r in rows]
    for item, r in zip(items, rows):
        # A reconcile-staged change to a live event is that event, not a
        # same-date duplicate of it.
        item["live_slug"] = r.get("townloops_slug") or (
            (parse_extra_json(r.get("extra_json")).get("reconcile") or {}).get("live_slug"))
    return _apply_judgement(conn, rows, autonomy.judge_items(items, page, ctx, llm))


class _SeedListing(NamedTuple):
    """One read of a seed: its events and the page text they came from, or
    why the read failed (`error`).
    `unparseable`: the LLM answered, but not with a JSON array."""
    events: list = []
    page: str = ""
    error: Optional[str] = None
    unparseable: bool = False


def _failed_read(action: str, url: str, err: str) -> _SeedListing:
    inspection.record(worker="event-find", action=action, target=url,
                      detail=f"failed: {err}", ok=False, error=err)
    return _SeedListing(error=err)


def _read_seed(profile: dict, seed: dict) -> _SeedListing:
    """The seed's listing, by its adapter (engine/seed_adapters) or the
    generic Firecrawl markdown → LLM path."""
    name, url = seed["name"], seed["url"]
    adapter = seed_adapters.adapter_of(seed)
    if adapter != seed_adapters.GENERIC:
        inspection.record(worker="event-find", action="adapter", target=url,
                          detail=f"reading seed {name} with the {adapter} adapter")
        try:
            got = seed_adapters.read(seed)
        except Exception as e:  # noqa: BLE001 — any adapter failure is a failed run
            err = f"{adapter}: {e}" if isinstance(e, seed_adapters.AdapterError) \
                else f"{adapter}: {type(e).__name__}: {e}"
            return _failed_read("adapter", url, err)
        return _SeedListing(got.events, got.page)

    inspection.record(worker="event-find", action="firecrawl", target=url,
                      detail=f"scraping seed {name}")
    ok, md, err = _firecrawl_listing(url)
    if not ok:
        return _failed_read("firecrawl", url, err)
    prompt = _build_extraction_prompt(profile, name, url, md)
    inspection.record(worker="event-find", action="llm-call", target=name,
                      detail=f"extracting events from {len(md)}-char markdown")
    codex_ok, out, err = _codex_exec(prompt)
    if not codex_ok:
        return _SeedListing(error=f"codex: {err}")
    events = _extracted_events(out, md)
    # An empty JSON array is a page listing nothing; anything else unparseable.
    return _SeedListing(events, md, unparseable=not events and not _EMPTY_LIST_RE.search(out or ""))


def cmd_find(args) -> int:
    # Closed on any exit: a crash mid-write must roll back here, not hold the
    # DB lock while main() records the failure for the dashboard.
    conn = sqlite3.connect(_DB_PATH)
    try:
        return _find(conn, args)
    finally:
        conn.close()


def _find(conn: sqlite3.Connection, args) -> int:
    profile = _load_town_profile()
    seeds = _seed_selection(conn, args.seed_id, args.limit)
    if not seeds:
        print(json.dumps({"error": "no approved seeds to scrape",
                          "hint": "approve seeds in the dashboard first"}))
        return 0

    total_inserted = 0
    total_updated = 0
    total_seeds = 0
    total_events_seen = 0
    health = source_health.settings_from(profile)
    mode = autonomy.settings(profile)
    judging = mode.autonomous and not args.dry_run
    judged = {"approved": 0, "dropped": 0, "deferred": 0}
    ctx = None
    if judging:
        duplicate, blocked, established = _series_checks(conn, mode)
        ctx = autonomy.judge_context(conn, profile, mode, series_duplicate=duplicate,
                                     series_blocked=blocked,
                                     series_established=established)

    def judge(rows, seen=frozenset(), page=None):
        for k, v in _judge(conn, rows, set(seen), page, ctx, _codex_exec).items():
            judged[k] += v
    health_changes: list[dict] = []

    def record_run(seed: dict, started: str, ok: bool, fetched: int = 0, inserted: int = 0,
                   updated: int = 0, error: Optional[str] = None, judge: bool = True) -> None:
        if args.dry_run:   # neither history nor judged
            return
        change = source_health.record_run(
            conn, seed, started=started, ended=_iso_utc_now(), ok=ok, fetched=fetched,
            inserted=inserted, updated=updated, error=error, settings=health, judge=judge)
        if change:
            health_changes.append(change)

    if not args.dry_run:
        # Rows from before dedup get their canonical uid (no-op once done).
        event_dedup.backfill(conn)
        conn.commit()

    for seed in seeds:
        total_seeds += 1
        started = _iso_utc_now()
        seed_name = seed["name"]

        listing = _read_seed(profile, seed)
        if listing.error:
            record_run(seed, started, False, error=listing.error[:400])
            conn.commit()
            print(json.dumps({"seed": seed_name, "error": listing.error[:200]}))
            continue
        events, md = listing.events, listing.page

        if not events:
            if listing.unparseable:   # extraction failed: says nothing about the source's health
                record_run(seed, started, True, error="codex returned no parseable JSON",
                           judge=False)
                note = "no events extracted"
            else:   # the page really lists nothing
                record_run(seed, started, True)
                note = "no events listed"
                if judging:
                    judge(_pending_rows(conn, "source = ?", (seed_ref(seed_name),)), (), md)
            conn.commit()
            print(json.dumps({"seed": seed_name, "events_seen": 0, "note": note}))
            continue

        inserted = 0
        updated = 0
        skipped = 0
        seen_uids: list[str] = []
        listed_dates: list[str] = []
        listed_cancelled: list[tuple[str, str, str]] = []
        for evt in events:
            if not isinstance(evt, dict):
                skipped += 1
                continue
            if not (evt.get("title") and evt.get("date")):
                skipped += 1
                continue
            if args.dry_run:
                continue
            # "CANCELLED: X" on the listing: the marker comes off so it is still
            # row X, which is flagged (live) or declined (not shipped yet).
            said = event_cancel.listing_statement(evt, md)
            if said:
                evt = {**evt, "title": event_cancel.strip_status(evt["title"])}
            action = _upsert_event(conn, seed_name, evt)
            uid = _event_uid(seed_name, evt["title"], evt["date"])
            seen_uids.append(uid)
            listed_dates.append(evt["date"])
            listed = event_cancel.record_listing(conn, uid, said, seed_name)
            if listed in ("flagged", "declined"):
                listed_cancelled.append((listed, uid, said))
            if action == "inserted":
                inserted += 1
            elif action == "updated":
                updated += 1

        total_inserted += inserted
        total_updated += updated
        total_events_seen += len(events)

        if not args.dry_run:
            # Upcoming rows this run should have listed but didn't gain a miss.
            event_cancel.record_seed_run(conn, seed_name, seen_uids, listed_dates)

        record_run(seed, started, True, len(events), inserted, updated)
        if judging:
            judge(_pending_rows(conn, "source = ?", (seed_ref(seed_name),)), seen_uids, md)
        ended = _iso_utc_now()
        if not args.dry_run:
            conn.execute(
                "UPDATE event_seeds SET last_scraped_at = ?, last_event_count = ?, "
                "consecutive_empty_scrapes = CASE WHEN ? = 0 THEN consecutive_empty_scrapes + 1 "
                "ELSE 0 END WHERE seed_id = ?",
                (ended, len(events), len(events), seed["seed_id"]),
            )
            conn.commit()
        # After the commit: the activity log is another connection to this DB.
        for listed, uid, said in listed_cancelled:
            inspection.record(worker="event-find", action=f"listed-cancelled-{listed}",
                              target=uid, detail=f"{seed_name}: {said}")

        print(json.dumps({
            "seed": seed_name, "events_seen": len(events),
            "inserted": inserted, "updated": updated, "skipped_malformed": skipped,
        }))

    if judging and args.seed_id is None:
        # Events no approved seed will list again (a retired or rejected
        # seed's, say) are judged on what they carry.
        judge(_pending_rows(conn, "source NOT IN (SELECT 'seed:' || name FROM event_seeds "
                                  "WHERE status = 'approved')", ()))

    vanished = {"flagged": [], "cleared": []}
    if not args.dry_run:
        threshold = int(profile.get("cancellation_miss_threshold")
                        or event_cancel.DEFAULT_MISS_THRESHOLD)
        vanished = event_cancel.flag_vanished(conn, threshold)
        conn.commit()
        for uid in vanished["flagged"]:
            inspection.record(worker="event-find", action="possibly-cancelled", target=uid,
                              detail="live event missing from all its sources — operator review")

    for change in health_changes:
        inspection.record(worker="event-find", action="source-health", target=change["seed"],
                          detail=f"{change['status']}: {change['note']}",
                          ok=change["status"] == "recovered")
    if health_changes:
        _telegram(source_health.alert_text(
            os.environ.get("TOWNLOOPS_TOWN_SLUG", "?"), health_changes))

    summary = {
        "seeds_scraped": total_seeds,
        "events_seen": total_events_seen,
        "inserted": total_inserted,
        "updated": total_updated,
        "possibly_cancelled": len(vanished["flagged"]),
        "cancel_flags_cleared": len(vanished["cleared"]),
        "source_health": [f"{c['seed']}: {c['status']}" for c in health_changes],
        "dry_run": args.dry_run,
        **({"autonomy": judged} if judging else {}),
    }
    print(json.dumps({"summary": summary}))
    inspection.record(
        worker="event-find", action="skill-invoke",
        target="find", detail=json.dumps(summary),
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="event-find")
    p.add_argument("--seed-id", type=int, help="scrape one seed by id (else all approved)")
    p.add_argument("--limit", type=int, help="cap seeds this run")
    p.add_argument("--dry-run", action="store_true", help="scrape + LLM without DB insert")
    p.set_defaults(func=cmd_find)
    args = p.parse_args(argv)
    # The cron wrapper detaches this run, so a slow pass can still be going
    # when the next fires (or the operator kicks one off) — one at a time.
    try:
        with _lock.acquire("event-find"):
            try:
                return args.func(args)
            except Exception as e:
                inspection.record(worker="event-find", action="run-failed", target="find",
                                  ok=False, error=f"{type(e).__name__}: {e}")
                raise
    except _lock.LockHeld as e:
        print(json.dumps({"skipped": f"previous run still active: {e}"}))
        inspection.record(worker="event-find", action="skipped-overlap", target="find",
                          detail=f"previous run still active ({e.holder})")
        return 0


if __name__ == "__main__":
    sys.exit(main())
