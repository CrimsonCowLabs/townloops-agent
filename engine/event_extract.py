"""Seed-page event extraction shared by event-find (daily) and
event-reconcile (weekly).

Lifted out of skills/business/event-find/find.py so both skills scrape,
prompt, parse and upsert identically — in particular `event_uid` must be
computed the same way or the two jobs would insert duplicate rows for
the same seed event.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

import requests

from engine.discovery import (
    FIRECRAWL_TIMEOUT_S,
    _firecrawl_base,
    _firecrawl_headers,
)
from engine.policy import firecrawl_bucket


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def firecrawl_markdown(url: str, timeout_s: int = FIRECRAWL_TIMEOUT_S) -> tuple[bool, str, str]:
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


def build_extraction_prompt(profile: dict, seed_name: str, seed_url: str,
                             markdown: str, extra_fields: str = "") -> str:
    """`extra_fields` is appended verbatim to the key list — one
    `  "key": description` line per field, same format as the base keys."""
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
{extra_fields}
Only extract events with a specific date. Skip generic listings like "meets every Tuesday" unless you can compute a concrete next date. Skip past events (before today).

Scraped page follows:
---
{markdown[:12000]}
---
"""


def codex_exec(prompt: str, timeout_s: int = 120) -> tuple[bool, str, str]:
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


def parse_json_array(stdout: str) -> list[dict]:
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


def normalize_title(t: str) -> str:
    return re.sub(r'\s+', ' ', (t or '').strip().lower())[:200]


def event_uid(seed_name: str, title: str, date_iso: str) -> str:
    key = f"{seed_name}::{normalize_title(title)}::{(date_iso or '')[:16]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def content_hash(evt: dict) -> str:
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


def upsert_event(conn: sqlite3.Connection, seed_name: str, evt: dict) -> str:
    """Insert or update an event. Returns 'inserted' | 'updated' | 'unchanged'."""
    now = _iso_utc_now()
    uid = event_uid(seed_name, evt.get("title") or "", evt.get("date") or "")
    chash = content_hash(evt)
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
