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
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

import requests
from dateutil import tz as _tz
from dateutil.parser import isoparse as _isoparse

from engine import categories as _cats
from engine import event_dedup
from engine.discovery import (
    FIRECRAWL_TIMEOUT_S,
    _firecrawl_base,
    _firecrawl_headers,
)
from engine.policy import firecrawl_bucket


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def town_tz_name() -> str:
    """IANA name of the town's timezone ($AGENT_TZ), read per call so a
    process never ships with a timezone frozen at import time."""
    return os.environ.get("AGENT_TZ", "America/Los_Angeles")


def to_utc(value) -> str | None:
    """An extracted ISO 8601 date/time as a UTC ISO string. Naive values are
    local time in the town's timezone; explicit offsets are kept. None for
    anything that isn't ISO — a lenient parser would fill missing parts
    ("Saturday", "7pm") from today and ship a made-up date."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = _isoparse(value.strip())
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.gettz(town_tz_name()))
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def utc_times(start, end) -> tuple[str | None, str | None]:
    """(start, end) as UTC; an end that isn't after the start is dropped."""
    start_utc, end_utc = to_utc(start), to_utc(end)
    if start_utc and end_utc and end_utc <= start_utc:
        end_utc = None
    return start_utc, end_utc


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
    `  "key": description` line per field, same format as the base keys.
    Categories are offered from the town's live categories snapshot."""
    town = profile.get("name") or profile.get("slug") or "unknown"
    categories = _cats.town_categories()
    aliases = ", ".join(profile.get("name_aliases") or [])
    return f"""You are extracting upcoming events for the town of "{town}".
Known area aliases (things a page might say when referring to this town or nearby areas): {aliases or '(none)'}

Below is the scraped markdown of "{seed_name}" ({seed_url}). Extract every upcoming event mentioned on this page.

Return ONLY a JSON array — no prose, no markdown, no code fence. Each object must have these keys (use null for missing fields, never omit):
  "title":       event name (concise)
  "date":        start date/time in ISO 8601 as local wall-clock time in {town_tz_name()}, with no UTC offset (e.g. "2026-08-15T19:00:00")
  "end_at":      end date/time in the same form, or null
  "venue":       venue name if stated (e.g. "Kings Beach Community Center"), else null
  "location":    street address / city if stated, else null
  "description": short description (first 400 chars max)
  "categories":  JSON array of every category that fits, most fitting first, using only these slugs (or [] if none fit):
{_cats.prompt_vocabulary(categories) or "      (no categories configured — use [])"}
  "tags":        JSON array of up to 5 short lowercase descriptive tags (e.g. "free", "all-ages", "dog-friendly", "21+"), or []
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


def _hash(fields: dict) -> str:
    return hashlib.sha1(json.dumps(fields, sort_keys=True).encode("utf-8")).hexdigest()


def _source_fields(evt: dict) -> dict:
    return {
        "title": evt.get("title") or "",
        "date": evt.get("date") or "",
        "end_at": evt.get("end_at") or "",
        "venue": evt.get("venue") or "",
        "location": evt.get("location") or "",
        "description": (evt.get("description") or "")[:1000],
    }


def content_hash(evt: dict) -> str:
    """Hash of what the source says. Categories/tags are left out — they are
    the extractor's classification and vary run to run."""
    return _hash(_source_fields(evt))


def _legacy_content_hash(evt: dict, category: str | None) -> str:
    """The hash before categories left it: the same fields plus the single
    extracted `category`, which the row stored as `category_slug`. A row
    whose `category_slug` was edited since won't match and reads as updated."""
    return _hash({**_source_fields(evt), "category": category or ""})


def upsert_event(conn: sqlite3.Connection, seed_name: str, evt: dict) -> str:
    """Insert or update an event. Returns 'inserted' | 'updated' | 'unchanged'.
    A new row is linked to its canonical event (engine/event_dedup), which may
    mark it a duplicate of a copy found via another seed.

    Categories and tags are the extractor's classification, not source
    content: they don't count as a change, and an existing row only gains
    them where it has none (an operator's or a previous pass's choice stays)."""
    now = _iso_utc_now()
    uid = event_uid(seed_name, evt.get("title") or "", evt.get("date") or "")
    chash = content_hash(evt)
    # A legacy single `category` is read when `categories` is absent.
    raw_cats = evt["categories"] if evt.get("categories") is not None else evt.get("category")
    cat_slugs, tags = _cats.classify(raw_cats, evt.get("tags"))
    cats_json, tags_json = _cats.to_json(cat_slugs), _cats.to_json(tags)
    primary = cat_slugs[0] if cat_slugs else None
    row = conn.execute(
        "SELECT content_hash, category_slug FROM events WHERE event_uid = ?", (uid,),
    ).fetchone()
    date_iso = evt.get("date") or ""
    date_day = date_iso[:10] if date_iso else ""
    date_utc, end_at_utc = utc_times(date_iso, evt.get("end_at"))
    if row is None:
        conn.execute(
            """
            INSERT INTO events (
              event_uid, source, source_kind, name, title, date, date_day,
              date_local, date_utc, end_at, end_at_local, end_at_utc,
              venue_name, venue_raw,
              location, location_raw, description, category_slug, category_slugs, tags,
              hosting_group, source_url,
              first_seen_at, last_seen_at, content_hash, last_modified_at,
              status, approval_status, state, state_updated_at, discovered_via
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 'pending', 'metadata-extracted', ?, ?)
            """,
            (
                uid, f"seed:{seed_name}", "scraped",
                evt.get("title") or "", evt.get("title") or "",
                date_iso, date_day, date_iso, date_utc,
                evt.get("end_at") or None, evt.get("end_at") or None, end_at_utc,
                evt.get("venue") or None, evt.get("venue") or None,
                evt.get("location") or None, evt.get("location") or None,
                (evt.get("description") or "")[:2000], primary, cats_json, tags_json,
                evt.get("venue") or None,  # hosting_group placeholder
                evt.get("source_url") or None,
                now, now, chash, now,
                now, f"seed:{seed_name}",
            ),
        )
        # Copies of this event from other seeds share one canonical uid, and
        # only one copy goes on to the operator and ship.
        event_dedup.link(conn, uid)
        return "inserted"
    fill_classification = (
        "category_slug = CASE WHEN category_slugs IS NULL AND ? IS NOT NULL THEN ? "
        "  ELSE category_slug END, "
        "category_slugs = COALESCE(category_slugs, ?), tags = COALESCE(tags, ?)"
    )
    fill_args = (cats_json, primary, cats_json, tags_json)
    if row[0] in (chash, _legacy_content_hash(evt, row[1])):
        # Rows written before the UTC columns were set get them on next sight;
        # rows hashed by the old formula move to the new one.
        conn.execute(
            "UPDATE events SET last_seen_at = ?, date_utc = COALESCE(date_utc, ?), "
            "  end_at_utc = COALESCE(end_at_utc, ?), content_hash = ?, "
            f"  {fill_classification} WHERE event_uid = ?",
            (now, date_utc, end_at_utc, chash, *fill_args, uid),
        )
        return "unchanged"
    conn.execute(
        f"""
        UPDATE events SET
          name = ?, title = ?, date = ?, date_day = ?, date_local = ?, date_utc = ?,
          end_at = ?, end_at_local = ?, end_at_utc = ?,
          venue_name = ?, venue_raw = ?, location = ?, location_raw = ?,
          description = ?, source_url = ?,
          last_seen_at = ?, content_hash = ?, last_modified_at = ?,
          {fill_classification}
        WHERE event_uid = ?
        """,
        (
            evt.get("title") or "", evt.get("title") or "",
            date_iso, date_day, date_iso, date_utc,
            evt.get("end_at") or None, evt.get("end_at") or None, end_at_utc,
            evt.get("venue") or None, evt.get("venue") or None,
            evt.get("location") or None, evt.get("location") or None,
            (evt.get("description") or "")[:2000],
            evt.get("source_url") or None,
            now, chash, now, *fill_args, uid,
        ),
    )
    return "updated"
