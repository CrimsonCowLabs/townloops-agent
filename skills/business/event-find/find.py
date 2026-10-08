#!/usr/bin/env python3
"""event-find — scrape approved event_seeds, LLM-extract upcoming events, insert as pending."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from engine import event_cancel, event_dedup, inspection
from engine.event_extract import (  # noqa: F401 — re-exported for callers/tests
    build_extraction_prompt as _build_extraction_prompt,
    codex_exec as _codex_exec,
    content_hash as _content_hash,
    event_uid as _event_uid,
    firecrawl_markdown as _firecrawl_markdown,
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


def _record_source_run(conn: sqlite3.Connection, seed_name: str,
                       started: str, ended: str, ok: bool,
                       fetched: int, inserted: int, updated: int,
                       error: Optional[str]) -> None:
    conn.execute(
        """
        INSERT INTO source_runs (source, started_at, ended_at, ok, fetched, inserted, updated, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (f"seed:{seed_name}", started, ended, 1 if ok else 0,
         fetched, inserted, updated, error),
    )


def cmd_find(args) -> int:
    profile = _load_town_profile()
    conn = sqlite3.connect(_DB_PATH)
    seeds = _seed_selection(conn, args.seed_id, args.limit)
    if not seeds:
        print(json.dumps({"error": "no approved seeds to scrape",
                          "hint": "approve seeds in the dashboard first"}))
        return 0

    total_inserted = 0
    total_updated = 0
    total_seeds = 0
    total_events_seen = 0
    if not args.dry_run:
        # Rows from before dedup get their canonical uid (no-op once done).
        event_dedup.backfill(conn)
        conn.commit()

    for seed in seeds:
        total_seeds += 1
        started = _iso_utc_now()
        seed_name = seed["name"]

        inspection.record(
            worker="event-find", action="firecrawl",
            target=seed["url"],
            detail=f"scraping seed {seed_name}",
        )
        ok, md, err = _firecrawl_markdown(seed["url"])
        if not ok:
            _record_source_run(conn, seed_name, started, _iso_utc_now(),
                               False, 0, 0, 0, err[:400])
            conn.commit()
            inspection.record(
                worker="event-find", action="firecrawl",
                target=seed["url"], detail=f"failed: {err}", ok=False, error=err,
            )
            print(json.dumps({"seed": seed_name, "error": err[:200]}))
            continue

        prompt = _build_extraction_prompt(profile, seed_name, seed["url"], md)
        inspection.record(
            worker="event-find", action="llm-call",
            target=seed_name,
            detail=f"extracting events from {len(md)}-char markdown",
        )
        codex_ok, out, err = _codex_exec(prompt)
        if not codex_ok:
            _record_source_run(conn, seed_name, started, _iso_utc_now(),
                               False, 0, 0, 0, err[:400])
            conn.commit()
            print(json.dumps({"seed": seed_name, "error": f"codex: {err[:200]}"}))
            continue

        events = _parse_json_array(out)
        if not events:
            _record_source_run(conn, seed_name, started, _iso_utc_now(),
                               True, 0, 0, 0, "codex returned no parseable JSON")
            conn.commit()
            print(json.dumps({"seed": seed_name, "events_seen": 0, "note": "no events extracted"}))
            continue

        inserted = 0
        updated = 0
        skipped = 0
        seen_uids: list[str] = []
        listed_dates: list[str] = []
        for evt in events:
            if not isinstance(evt, dict):
                skipped += 1
                continue
            if not (evt.get("title") and evt.get("date")):
                skipped += 1
                continue
            if args.dry_run:
                continue
            action = _upsert_event(conn, seed_name, evt)
            seen_uids.append(_event_uid(seed_name, evt["title"], evt["date"]))
            listed_dates.append(evt["date"])
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

        ended = _iso_utc_now()
        _record_source_run(conn, seed_name, started, ended,
                           True, len(events), inserted, updated, None)
        conn.execute(
            "UPDATE event_seeds SET last_scraped_at = ?, last_event_count = ?, "
            "consecutive_empty_scrapes = CASE WHEN ? = 0 THEN consecutive_empty_scrapes + 1 ELSE 0 END "
            "WHERE seed_id = ?",
            (ended, len(events), len(events), seed["seed_id"]),
        )
        conn.commit()

        print(json.dumps({
            "seed": seed_name, "events_seen": len(events),
            "inserted": inserted, "updated": updated, "skipped_malformed": skipped,
        }))

    vanished = {"flagged": [], "cleared": []}
    if not args.dry_run:
        threshold = int(profile.get("cancellation_miss_threshold")
                        or event_cancel.DEFAULT_MISS_THRESHOLD)
        vanished = event_cancel.flag_vanished(conn, threshold)
        conn.commit()
        for uid in vanished["flagged"]:
            inspection.record(worker="event-find", action="possibly-cancelled", target=uid,
                              detail="live event missing from all its sources — operator review")

    summary = {
        "seeds_scraped": total_seeds,
        "events_seen": total_events_seen,
        "inserted": total_inserted,
        "updated": total_updated,
        "possibly_cancelled": len(vanished["flagged"]),
        "cancel_flags_cleared": len(vanished["cleared"]),
        "dry_run": args.dry_run,
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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
