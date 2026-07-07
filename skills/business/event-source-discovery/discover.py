#!/usr/bin/env python3
"""event-source-discovery — build the per-town sources.yaml.

The agent drives the search (using its own web tools); this CLI persists
candidates, probes them for machine-readable feeds, and compiles the
approved set into a sources.yaml compatible with event-ingest.

Workflow (agent-facing):
  1. Agent runs web searches ("<town> chamber events calendar", etc.)
     and calls `discover.py add <url> --title "..."` for each hit.
  2. `discover.py probe --limit 20` — try each candidate's URL for iCal,
     JSON-LD Event, RSS/Atom. Flags successful probes as `needs_review`.
  3. Agent asks the user to approve/reject via chat, then runs
     `discover.py approve <url>` or `reject <url> --reason "..."`.
  4. `discover.py compile` regenerates sources.yaml (idempotent).

Data:
  state/discovery.db  — candidates + probes
  state/sources.yaml  — compiled output, input to event-ingest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
import yaml

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
STATE_DIR = HERMES_HOME / "state"
DISCOVERY_DB = Path(os.environ.get("DISCOVERY_DB_PATH", STATE_DIR / "discovery.db"))
SOURCES_YAML = Path(os.environ.get("SOURCES_YAML_PATH", STATE_DIR / "sources.yaml"))

UA = "townloops-agent/1.0 (source-discovery)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    DISCOVERY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DISCOVERY_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidates (
          url               TEXT PRIMARY KEY,
          title             TEXT,
          discovery_method  TEXT,
          status            TEXT NOT NULL DEFAULT 'proposed',
          notes             TEXT,
          added_at          TEXT NOT NULL,
          approved_feed_url TEXT,
          approved_parser   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);

        CREATE TABLE IF NOT EXISTS probes (
          url             TEXT PRIMARY KEY,
          probed_at       TEXT NOT NULL,
          http_status     INTEGER,
          ical_url        TEXT,
          jsonld_events   INTEGER DEFAULT 0,
          rss_url         TEXT,
          html_calendar   INTEGER DEFAULT 0,
          notes           TEXT
        );
        """
    )
    conn.commit()
    return conn


# --------- add / list / approve / reject ---------

def cmd_add(args) -> int:
    conn = _connect()
    conn.execute(
        """
        INSERT INTO candidates(url, title, discovery_method, status, notes, added_at)
        VALUES (?, ?, ?, 'proposed', ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title = COALESCE(excluded.title, candidates.title),
            discovery_method = COALESCE(excluded.discovery_method, candidates.discovery_method)
        """,
        (args.url, args.title, args.method, args.notes, _now()),
    )
    conn.commit()
    print(json.dumps({"url": args.url, "status": "added"}))
    return 0


def cmd_list(args) -> int:
    conn = _connect()
    if args.status:
        rows = conn.execute(
            "SELECT * FROM candidates WHERE status = ? ORDER BY added_at",
            (args.status,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM candidates ORDER BY status, added_at").fetchall()
    for r in rows:
        probe = conn.execute("SELECT * FROM probes WHERE url = ?", (r["url"],)).fetchone()
        d = dict(r)
        d["probe"] = dict(probe) if probe else None
        print(json.dumps(d))
    return 0


def cmd_approve(args) -> int:
    conn = _connect()
    # Prefer the probe's detected feed if the user didn't pick one manually.
    feed = args.feed_url
    parser = args.parser
    if not feed or not parser:
        probe = conn.execute("SELECT * FROM probes WHERE url = ?", (args.url,)).fetchone()
        if probe:
            if not feed:
                feed = probe["ical_url"] or probe["rss_url"] or args.url
            if not parser:
                if probe["ical_url"]:
                    parser = "ical"
                elif probe["rss_url"]:
                    parser = "rss"
                elif probe["jsonld_events"]:
                    parser = "jsonld"
                else:
                    parser = "firecrawl"
    if not feed:
        feed = args.url
    if not parser:
        parser = "firecrawl"
    conn.execute(
        "UPDATE candidates SET status='approved', approved_feed_url=?, approved_parser=? "
        "WHERE url = ?",
        (feed, parser, args.url),
    )
    conn.commit()
    print(json.dumps({"url": args.url, "approved_feed_url": feed, "parser": parser}))
    return 0


def cmd_reject(args) -> int:
    conn = _connect()
    conn.execute(
        "UPDATE candidates SET status='rejected', notes = COALESCE(notes,'') || ' | reject: ' || ? "
        "WHERE url = ?",
        (args.reason or "", args.url),
    )
    conn.commit()
    print(json.dumps({"url": args.url, "status": "rejected"}))
    return 0


# --------- probe ---------

_HTML_ICAL_RE = re.compile(
    r'<link[^>]+rel=["\']alternate["\'][^>]+type=["\']text/calendar["\'][^>]+href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_HTML_RSS_RE = re.compile(
    r'<link[^>]+rel=["\']alternate["\'][^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]+href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_LDJSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _try_get(url: str, timeout: float = 15.0) -> Optional[requests.Response]:
    try:
        return requests.get(url, timeout=timeout, headers={"User-Agent": UA})
    except requests.RequestException:
        return None


def _probe_ical(base_url: str, html: str) -> Optional[str]:
    """Try to find an iCal feed at the URL or in the HTML."""
    for suffix in ("?ical=1", "/calendar.ics", "/events.ics", "/ical", "/feed.ics"):
        candidate = base_url.rstrip("/") + suffix
        r = _try_get(candidate)
        if r and r.status_code < 400 and (
            r.headers.get("content-type", "").startswith("text/calendar")
            or r.text.lstrip().startswith("BEGIN:VCALENDAR")
        ):
            return candidate
    m = _HTML_ICAL_RE.search(html)
    if m:
        return urljoin(base_url, m.group(1))
    return None


def _probe_rss(base_url: str, html: str) -> Optional[str]:
    m = _HTML_RSS_RE.search(html)
    if m:
        return urljoin(base_url, m.group(1))
    for suffix in ("/feed", "/rss", "/rss.xml", "/atom.xml"):
        candidate = base_url.rstrip("/") + suffix
        r = _try_get(candidate)
        if r and r.status_code < 400 and (
            "xml" in r.headers.get("content-type", "")
            or r.text.lstrip().startswith(("<?xml", "<rss", "<feed"))
        ):
            return candidate
    return None


def _probe_jsonld_events(html: str) -> int:
    count = 0
    for raw in _LDJSON_RE.findall(html):
        try:
            block = json.loads(raw)
        except json.JSONDecodeError:
            continue

        def walk(node: Any) -> None:
            nonlocal count
            if isinstance(node, list):
                for n in node:
                    walk(n)
            elif isinstance(node, dict):
                t = node.get("@type")
                if t == "Event" or (isinstance(t, list) and "Event" in t):
                    count += 1
                for v in node.values():
                    walk(v)

        walk(block)
    return count


def _probe_html_calendar(html: str) -> bool:
    """Rough heuristic: pages containing a table/list with dated event blocks."""
    low = html.lower()
    return any(m in low for m in ("class=\"event", "eventtitle", "event-list", "fc-event"))


def cmd_probe(args) -> int:
    conn = _connect()
    q = "SELECT url FROM candidates WHERE status='proposed'"
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    urls = [r["url"] for r in conn.execute(q).fetchall()]
    for url in urls:
        r = _try_get(url)
        if not r or r.status_code >= 400:
            conn.execute(
                "INSERT OR REPLACE INTO probes(url, probed_at, http_status, notes) "
                "VALUES (?, ?, ?, ?)",
                (url, _now(), r.status_code if r else 0, "fetch failed"),
            )
            conn.commit()
            print(json.dumps({"url": url, "http": r.status_code if r else 0, "probe": "fail"}))
            continue
        html_body = r.text
        base = r.url
        ical = _probe_ical(base, html_body)
        rss = _probe_rss(base, html_body)
        jsonld = _probe_jsonld_events(html_body)
        html_cal = _probe_html_calendar(html_body)
        conn.execute(
            "INSERT OR REPLACE INTO probes(url, probed_at, http_status, ical_url, "
            "jsonld_events, rss_url, html_calendar) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, _now(), r.status_code, ical, jsonld, rss, int(html_cal)),
        )
        # Flip status if we found something.
        if ical or rss or jsonld or html_cal:
            conn.execute(
                "UPDATE candidates SET status='needs_review' WHERE url = ?", (url,)
            )
        conn.commit()
        print(
            json.dumps(
                {
                    "url": url,
                    "http": r.status_code,
                    "ical": ical,
                    "rss": rss,
                    "jsonld_events": jsonld,
                    "html_calendar": html_cal,
                }
            )
        )
    return 0


# --------- compile ---------

def cmd_compile(args) -> int:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM candidates WHERE status='approved' ORDER BY added_at"
    ).fetchall()
    entries = []
    for r in rows:
        entry = {
            "name": (r["title"] or urlparse(r["url"]).hostname or r["url"])[:80],
            "type": r["approved_parser"] or "firecrawl",
            "url": r["approved_feed_url"] or r["url"],
        }
        if r["notes"]:
            entry["notes"] = r["notes"]
        entries.append(entry)
    doc = {
        "generated_at": _now(),
        "town_slug": os.environ.get("TOWNLOOPS_TOWN_SLUG", "?"),
        "sources": entries,
    }
    SOURCES_YAML.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_YAML.write_text(yaml.safe_dump(doc, sort_keys=False))
    print(json.dumps({"path": str(SOURCES_YAML), "sources": len(entries)}))
    return 0


# --------- main ---------

def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="record a candidate source URL")
    a.add_argument("url")
    a.add_argument("--title")
    a.add_argument("--method", help="how you found it (search:xyz, linkfollow:abc)")
    a.add_argument("--notes")
    a.set_defaults(func=cmd_add)

    lp = sub.add_parser("list", help="list candidates")
    lp.add_argument("--status", choices=["proposed", "needs_review", "approved", "rejected"])
    lp.set_defaults(func=cmd_list)

    ap = sub.add_parser("approve", help="approve a candidate")
    ap.add_argument("url")
    ap.add_argument("--feed-url", dest="feed_url", help="override detected feed")
    ap.add_argument("--parser", choices=["ical", "jsonld", "rss", "html", "firecrawl"])
    ap.set_defaults(func=cmd_approve)

    rj = sub.add_parser("reject", help="reject a candidate")
    rj.add_argument("url")
    rj.add_argument("--reason")
    rj.set_defaults(func=cmd_reject)

    pb = sub.add_parser("probe", help="probe candidates for iCal / JSON-LD / RSS")
    pb.add_argument("--limit", type=int)
    pb.set_defaults(func=cmd_probe)

    cp = sub.add_parser("compile", help="regenerate sources.yaml from approved candidates")
    cp.set_defaults(func=cmd_compile)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
