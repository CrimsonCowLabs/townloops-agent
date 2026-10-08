#!/usr/bin/env python3
"""event-seed-discovery — LLM proposes seed source URLs; HEAD-verified; inserted as proposed."""

from __future__ import annotations

import argparse
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
from urllib.parse import urlparse

import yaml

from engine import event_dedup, feed_probe, inspection, source_health


VALID_KINDS = {"chamber", "government", "tourism", "library",
               "parks", "newspaper", "venue", "other"}

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


def _build_prompt(profile: dict) -> str:
    town = profile.get("name") or profile.get("slug") or "unknown"
    region = profile.get("region") or ""
    aliases = ", ".join(profile.get("name_aliases") or [])
    kinds = ", ".join(sorted(VALID_KINDS))
    return f"""You are helping a local-events editor for the town of "{town}"{" (" + region + ")" if region else ""}.
Known area aliases: {aliases or '(none)'}

Task: propose seed websites where the local editor could reliably find upcoming
community events for this area. Include the town's chamber of commerce, city
and county government pages that publish events/meetings, the public library
system, parks & recreation, local tourism board, prominent event venues
(theaters, community centers), and the local newspaper's events section.

Return ONLY a JSON array of objects — no prose, no markdown, no code fence.
Each object must have exactly these three keys:
  "name":  short human-readable label
  "url":   full https:// URL to the site's events / calendar page (or homepage if no separate events URL exists)
  "kind":  one of: {kinds}

Prefer specific events-page URLs over homepages. Skip generic listings
(Eventbrite, AllEvents.in) — those are aggregators, not local sources.
Return 8-15 seeds. Real, verifiable URLs only — do not invent hostnames
you're unsure about.
"""


def _codex_exec(prompt: str, timeout_s: int = 90) -> tuple[bool, str, str]:
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
    """Codex emits some header text + the actual output on stdout with
    --ephemeral. The JSON array is usually the last multi-line JSON. Find
    it via bracket-balanced scan."""
    if not stdout:
        return []
    # Try last line first
    for line in reversed(stdout.splitlines()):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    # Fall back: find the largest bracket-balanced substring
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
    return []


def _scrape_page(url: str) -> tuple[bool, int, str, str]:
    """Fetch the seed page through Firecrawl (rawHtml) → (ok, status code,
    html, final URL after redirects). Firecrawl bypasses squid, so this
    works on any hostname, not just allowlisted ones. A 2xx/3xx verifies the
    seed; the html is what feed probing reads."""
    from engine.discovery import _firecrawl_base, _firecrawl_headers, FIRECRAWL_TIMEOUT_S
    from engine.policy import firecrawl_bucket
    import requests
    firecrawl_bucket.wait()
    with inspection.timed(worker="event-seed-discovery",
                          action="firecrawl-head",
                          target=url,
                          detail="verifying seed URL") as ctx:
        try:
            r = requests.post(
                f"{_firecrawl_base()}/v1/scrape",
                headers=_firecrawl_headers(),
                json={"url": url, "formats": ["rawHtml"]},
                timeout=FIRECRAWL_TIMEOUT_S,
            )
        except requests.RequestException as e:
            ctx["ok"] = False
            ctx["error"] = str(e)
            return (False, 0, "", url)
        if not r.ok:
            return (False, r.status_code, "", url)
        try:
            body = r.json()
        except ValueError:
            return (False, r.status_code, "", url)
        data = body.get("data") or {}
        meta = data.get("metadata") or {}
        code = int(meta.get("statusCode") or 200)
        ctx["detail"] = f"verified status={code}"
        final = meta.get("url") or meta.get("sourceURL") or url
        return (200 <= code < 400, code, data.get("rawHtml") or "", final)


def _record_feed(conn: sqlite3.Connection, seed_url: str, html: str, final_url: str) -> dict:
    """Probe the seed's page for a machine-readable feed and record it."""
    found = feed_probe.detect(html, final_url)
    detail = {"feeds": found["feeds"]}
    if final_url != seed_url:   # short link / redirect: feeds resolve against the target
        detail["resolved_url"] = final_url
    conn.execute(
        "UPDATE event_seeds SET feed_type = ?, feed_url = ?, feed_detail = ?, feed_probed_at = ? "
        "WHERE url = ?",
        (found["type"], found["url"], json.dumps(detail), _iso_utc_now(), seed_url))
    return found


def _valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def cmd_discover(args) -> int:
    profile = _load_town_profile()
    if not profile.get("name") and not profile.get("slug"):
        print(json.dumps({"error": "town-profile.yaml missing name/slug"}))
        return 2

    prompt = _build_prompt(profile)
    inspection.record(
        worker="event-seed-discovery", action="llm-call",
        target="seed-discovery",
        detail=f"asking Codex for seeds — town={profile.get('name')}, region={profile.get('region')}",
    )
    ok, out, err = _codex_exec(prompt)
    if not ok:
        msg = f"codex failed: {err[:200]}"
        inspection.record(
            worker="event-seed-discovery", action="skill-invoke",
            target="discover", detail=msg, ok=False, error=err[:400],
        )
        print(json.dumps({"error": msg}))
        return 3
    proposals = _parse_json_array(out)
    if not proposals:
        msg = "codex returned no parseable JSON array"
        inspection.record(
            worker="event-seed-discovery", action="skill-invoke",
            target="discover", detail=msg, ok=False,
            error=f"stdout_tail={(out or '')[-200:]}",
        )
        print(json.dumps({"error": msg, "stdout_tail": (out or "")[-500:]}))
        return 4

    if args.dry_run:
        print(json.dumps({"proposals": proposals, "count": len(proposals)}, indent=2))
        return 0

    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    now = _iso_utc_now()
    already: set[str] = {
        r["url"] for r in conn.execute("SELECT url FROM event_seeds")
    }

    inserted = 0
    verified_fails = 0
    duplicates = 0
    bad_shape = 0
    with_feed = 0
    for p in proposals[:args.limit]:
        if not isinstance(p, dict):
            bad_shape += 1
            continue
        name = (p.get("name") or "").strip()
        url = (p.get("url") or "").strip()
        kind = (p.get("kind") or "other").strip().lower()
        if not name or not _valid_url(url):
            bad_shape += 1
            continue
        if kind not in VALID_KINDS:
            kind = "other"
        if url in already:
            duplicates += 1
            continue
        ok, code, html, final_url = _scrape_page(url)
        if not ok:
            verified_fails += 1
            continue
        conn.execute(
            """
            INSERT INTO event_seeds
              (name, url, kind, status, discovered_by,
               first_seen_at, last_seen_at)
            VALUES (?, ?, ?, 'proposed', 'llm', ?, ?)
            """,
            (name[:200], url, kind, now, now),
        )
        if _record_feed(conn, url, html, final_url)["url"]:
            with_feed += 1
        already.add(url)
        inserted += 1
    conn.commit()
    conn.close()

    summary = {
        "proposed_by_llm": len(proposals),
        "inserted": inserted,
        "duplicates_skipped": duplicates,
        "url_verification_failed": verified_fails,
        "malformed_llm_output": bad_shape,
        "with_feed": with_feed,
    }
    inspection.record(
        worker="event-seed-discovery", action="skill-invoke",
        target="discover", detail=json.dumps(summary), ok=True,
    )
    print(json.dumps(summary))
    return 0


def cmd_probe(args) -> int:
    """Probe seeds' pages for a feed: those never probed (else `--all`, or
    one `--seed-id`), rejected seeds excepted. Seeds never attempted go
    before ones that were unreachable last time; `--limit` caps the run."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    q = "SELECT seed_id, name, url FROM event_seeds WHERE status != 'rejected'"
    params: list = []
    if args.seed_id is not None:
        q += " AND seed_id = ?"
        params.append(args.seed_id)
    elif not args.all:
        q += " AND feed_probed_at IS NULL"
    q += " ORDER BY feed_detail IS NOT NULL, seed_id"
    if args.limit:
        q += " LIMIT ?"
        params.append(args.limit)
    seeds = [dict(r) for r in conn.execute(q, params)]
    counts = {"probed": 0, "with_feed": 0, "unreachable": 0}
    for seed in seeds:
        ok, code, html, final_url = _scrape_page(seed["url"])
        if not ok:   # stays unprobed; the next probe retries it
            conn.execute("UPDATE event_seeds SET feed_detail = ? "
                         "WHERE seed_id = ? AND feed_probed_at IS NULL",
                         (json.dumps({"error": f"unreachable (status {code})",
                                      "at": _iso_utc_now()}), seed["seed_id"]))
            conn.commit()
            counts["unreachable"] += 1
            print(json.dumps({"seed": seed["name"], "error": f"unreachable (status {code})"}))
            continue
        found = _record_feed(conn, seed["url"], html, final_url)
        conn.commit()
        counts["probed"] += 1
        counts["with_feed"] += bool(found["url"])
        print(json.dumps({"seed": seed["name"], "feed_type": found["type"],
                          "feed_url": found["url"]}))
    conn.close()
    inspection.record(worker="event-seed-discovery", action="skill-invoke",
                      target="probe", detail=json.dumps(counts), ok=True)
    print(json.dumps(counts))
    return 0


def cmd_show(args) -> int:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    for r in conn.execute(
        "SELECT seed_id, name, url, kind, status, source_role, last_scraped_at, "
        "  feed_type, feed_url, health_status, health_note "
        "FROM event_seeds ORDER BY status, kind, name"
    ):
        row = dict(r)
        row["role"] = event_dedup.seed_role(row)  # effective: source_role, else inferred
        print(json.dumps(row))
    conn.close()
    return 0


def cmd_set_role(args) -> int:
    """Mark a seed as a primary source or a syndicator (copies of an event
    from a primary source win dedup), or `auto` to infer it again."""
    conn = sqlite3.connect(_DB_PATH)
    role = None if args.role == "auto" else args.role
    cur = conn.execute("UPDATE event_seeds SET source_role = ? WHERE seed_id = ?",
                       (role, args.seed_id))
    conn.commit()
    conn.close()
    if not cur.rowcount:
        print(json.dumps({"error": f"no seed {args.seed_id}"}))
        return 1
    print(json.dumps({"seed_id": args.seed_id, "source_role": role}))
    return 0


def cmd_health(args) -> int:
    """List seeds with a source-health alert, or `--ack` one: accept its
    current level as normal (earlier runs stop counting)."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    if args.ack is not None:
        if not source_health.ack(conn, args.ack):
            print(json.dumps({"error": f"no seed {args.ack}"}))
            conn.close()
            return 1
        print(json.dumps({"seed_id": args.ack, "health": "acknowledged"}))
    else:
        for r in source_health.flagged(conn):
            print(json.dumps({k: r[k] for k in ("seed_id", "name", "url", "health_status",
                                                "health_note", "health_flagged_at")}))
    conn.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="event-seed-discovery")
    sub = p.add_subparsers(dest="cmd")
    d = sub.add_parser("discover", help="LLM-propose seeds, verify, insert")
    d.add_argument("--limit", type=int, default=15)
    d.add_argument("--dry-run", action="store_true")
    d.set_defaults(func=cmd_discover)
    pr = sub.add_parser("probe", help="detect machine-readable feeds on seed pages")
    pr.add_argument("--seed-id", type=int, help="probe one seed (even if probed before)")
    pr.add_argument("--all", action="store_true", help="re-probe every non-rejected seed")
    pr.add_argument("--limit", type=int, help="cap seeds this run")
    pr.set_defaults(func=cmd_probe)
    s = sub.add_parser("show", help="print current event_seeds rows as JSON")
    s.set_defaults(func=cmd_show)
    r = sub.add_parser("set-role", help="mark a seed primary / syndicator (or auto)")
    r.add_argument("--seed-id", type=int, required=True)
    r.add_argument("--role", required=True, choices=("primary", "syndicator", "auto"))
    r.set_defaults(func=cmd_set_role)
    h = sub.add_parser("health", help="list source-health alerts, or --ack one")
    h.add_argument("--ack", type=int, metavar="SEED_ID",
                   help="accept the seed's current event count as its new normal")
    h.set_defaults(func=cmd_health)
    # Default subcommand is `discover` when none given
    args = p.parse_args(argv)
    if not args.cmd:
        args = p.parse_args(["discover", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
