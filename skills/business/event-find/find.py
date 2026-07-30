#!/usr/bin/env python3
"""event-find — scrape approved event_seeds, LLM-extract upcoming events, insert as pending."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml

from engine import inspection
from engine.discovery import (
    FIRECRAWL_TIMEOUT_S,
    _firecrawl_base,
    _firecrawl_headers,
)
from engine.policy import firecrawl_bucket


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


def _firecrawl_markdown(url: str, timeout_s: int = FIRECRAWL_TIMEOUT_S) -> tuple[bool, str, str]:
    """Fetch a page as markdown via Firecrawl. Returns (ok, markdown, error).

    waitFor: 3500ms — many town calendars are ASP.NET / JS-rendered widgets
    (Placer County Calendar.aspx, etc.) that show an ajax-loader placeholder
    on first paint and only fill in via JS. Without a wait, Firecrawl-in-
    markdown returns the pre-render skeleton and the LLM sees no events."""
    firecrawl_bucket.wait()
    try:
        r = requests.post(
            f"{_firecrawl_base()}/v1/scrape",
            headers=_firecrawl_headers(),
            json={
                "url": url,
                "formats": ["markdown"],
                "waitFor": 3500,
            },
            timeout=timeout_s + 5,
        )
    except requests.RequestException as e:
        return (False, "", f"{type(e).__name__}: {e}")
    if not r.ok:
        return (False, "", f"firecrawl http {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        return (False, "", "firecrawl returned non-JSON")
    md = (body.get("data") or {}).get("markdown") or ""
    return (bool(md), md, "" if md else "empty markdown")


def _build_extraction_prompt(profile: dict, seed_name: str, seed_url: str,
                             markdown: str) -> str:
    town = profile.get("name") or profile.get("slug") or "unknown"
    aliases = ", ".join(profile.get("name_aliases") or [])
    return f"""You are extracting upcoming events for the town of "{town}".
Known area aliases (things a page might say when referring to this town or nearby areas): {aliases or '(none)'}

Below is the scraped markdown of "{seed_name}" ({seed_url}). Extract every upcoming event mentioned on this page.

Return ONLY a JSON array — no prose, no markdown, no code fence. Each object must have these keys (use null for missing fields, never omit):
  "title":       event name (concise)
  "date":        start date/time in ISO 8601 (e.g. "2026-08-15T19:00:00" — infer timezone as America/Los_Angeles if not stated)
  "end_at":      end date/time in ISO 8601, or null
  "venue":       venue name if stated (e.g. "Kings Beach Community Center"), else null
  "location":    street address / city if stated, else null
  "description": short description (first 400 chars max)
  "category":    one of: music, arts, community, food, outdoors, education, sports, family, government, other
  "source_url":  the URL on this page that links to the event's own detail page, or null if not present

Only extract events with a specific date. Skip generic listings like "meets every Tuesday" unless you can compute a concrete next date. Skip past events (before today).

Scraped page follows:
---
{markdown[:12000]}
---
"""


def _codex_exec(prompt: str, timeout_s: int = 120) -> tuple[bool, str, str]:
    if not shutil.which("codex"):
        return (False, "", "codex CLI not on PATH")
    cmd = [
        "codex", "exec",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--ephemeral",
        prompt,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return (False, "", f"codex exec timed out after {timeout_s}s")
    if proc.returncode != 0:
        return (False, "", (proc.stderr or "")[:500])
    return (True, proc.stdout or "", "")


def _parse_json_array(stdout: str) -> list[dict]:
    if not stdout:
        return []
    # Try trailing bracket-balanced substring (largest)
    starts = [i for i, ch in enumerate(stdout) if ch == "["]
    for start in starts:
        depth = 0
        for j in range(start, len(stdout)):
            ch = stdout[j]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    chunk = stdout[start:j + 1]
                    try:
                        parsed = json.loads(chunk)
                        if isinstance(parsed, list):
                            return parsed
                    except json.JSONDecodeError:
                        break
    # Try last line
    for line in reversed(stdout.splitlines()):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    return []


def _normalize_title(t: str) -> str:
    return re.sub(r'\s+', ' ', (t or '').strip().lower())[:200]


def _event_uid(seed_name: str, title: str, date_iso: str) -> str:
    key = f"{seed_name}::{_normalize_title(title)}::{(date_iso or '')[:16]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _content_hash(evt: dict) -> str:
    key = json.dumps({
        "title": evt.get("title") or "",
        "date": evt.get("date") or "",
        "end_at": evt.get("end_at") or "",
        "venue": evt.get("venue") or "",
        "location": evt.get("location") or "",
        "description": (evt.get("description") or "")[:1000],
        "category": evt.get("category") or "",
    }, sort_keys=True)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


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


def _upsert_event(conn: sqlite3.Connection, seed_name: str, evt: dict) -> str:
    """Insert or update an event. Returns 'inserted' | 'updated' | 'unchanged'."""
    now = _iso_utc_now()
    uid = _event_uid(seed_name, evt.get("title") or "", evt.get("date") or "")
    chash = _content_hash(evt)
    row = conn.execute(
        "SELECT content_hash FROM events WHERE event_uid = ?", (uid,),
    ).fetchone()
    date_iso = evt.get("date") or ""
    date_day = date_iso[:10] if date_iso else ""
    if row is None:
        conn.execute(
            """
            INSERT INTO events (
              event_uid, source, source_kind, name, title, date, date_day,
              date_local, end_at, end_at_local, venue_name, venue_raw,
              location, location_raw, description, category_slug,
              hosting_group, source_url,
              first_seen_at, last_seen_at, content_hash, last_modified_at,
              status, approval_status, state, state_updated_at, discovered_via
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 'pending', 'metadata-extracted', ?, ?)
            """,
            (
                uid, f"seed:{seed_name}", "scraped",
                evt.get("title") or "", evt.get("title") or "",
                date_iso, date_day, date_iso,
                evt.get("end_at") or None, evt.get("end_at") or None,
                evt.get("venue") or None, evt.get("venue") or None,
                evt.get("location") or None, evt.get("location") or None,
                (evt.get("description") or "")[:2000], evt.get("category") or None,
                evt.get("venue") or None,  # hosting_group placeholder
                evt.get("source_url") or None,
                now, now, chash, now,
                now, f"seed:{seed_name}",
            ),
        )
        return "inserted"
    if row[0] == chash:
        conn.execute(
            "UPDATE events SET last_seen_at = ? WHERE event_uid = ?",
            (now, uid),
        )
        return "unchanged"
    conn.execute(
        """
        UPDATE events SET
          name = ?, title = ?, date = ?, date_day = ?, date_local = ?,
          end_at = ?, end_at_local = ?,
          venue_name = ?, venue_raw = ?, location = ?, location_raw = ?,
          description = ?, category_slug = ?, source_url = ?,
          last_seen_at = ?, content_hash = ?, last_modified_at = ?
        WHERE event_uid = ?
        """,
        (
            evt.get("title") or "", evt.get("title") or "",
            date_iso, date_day, date_iso,
            evt.get("end_at") or None, evt.get("end_at") or None,
            evt.get("venue") or None, evt.get("venue") or None,
            evt.get("location") or None, evt.get("location") or None,
            (evt.get("description") or "")[:2000], evt.get("category") or None,
            evt.get("source_url") or None,
            now, chash, now, uid,
        ),
    )
    return "updated"


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
            if action == "inserted":
                inserted += 1
            elif action == "updated":
                updated += 1

        total_inserted += inserted
        total_updated += updated
        total_events_seen += len(events)

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

    summary = {
        "seeds_scraped": total_seeds,
        "events_seen": total_events_seen,
        "inserted": total_inserted,
        "updated": total_updated,
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
