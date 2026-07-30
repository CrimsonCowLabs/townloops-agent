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

from engine import inspection


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


def _head_verify(url: str) -> tuple[bool, int]:
    """Send a Firecrawl scrape and inspect the returned status code.
    Firecrawl bypasses squid; using it here means the HEAD-check works
    on any hostname, not just allowlisted ones."""
    from engine.discovery import _firecrawl_base, _firecrawl_headers, FIRECRAWL_TIMEOUT_S
    from engine.policy import firecrawl_bucket
    import requests
    firecrawl_bucket.wait()
    with inspection.timed(worker="event-seed-discovery",
                          action="firecrawl-head",
                          target=url,
                          detail=f"verifying LLM-proposed seed URL") as ctx:
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
            return (False, 0)
        if not r.ok:
            return (False, r.status_code)
        try:
            body = r.json()
        except ValueError:
            return (False, r.status_code)
        meta = (body.get("data") or {}).get("metadata") or {}
        code = int(meta.get("statusCode") or 200)
        ctx["detail"] = f"verified status={code}"
        return (200 <= code < 400, code)


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
        ok, code = _head_verify(url)
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
    }
    inspection.record(
        worker="event-seed-discovery", action="skill-invoke",
        target="discover", detail=json.dumps(summary), ok=True,
    )
    print(json.dumps(summary))
    return 0


def cmd_show(args) -> int:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    for r in conn.execute(
        "SELECT seed_id, name, url, kind, status, last_scraped_at "
        "FROM event_seeds ORDER BY status, kind, name"
    ):
        print(json.dumps(dict(r)))
    conn.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="event-seed-discovery")
    sub = p.add_subparsers(dest="cmd")
    d = sub.add_parser("discover", help="LLM-propose seeds, verify, insert")
    d.add_argument("--limit", type=int, default=15)
    d.add_argument("--dry-run", action="store_true")
    d.set_defaults(func=cmd_discover)
    s = sub.add_parser("show", help="print current event_seeds rows as JSON")
    s.set_defaults(func=cmd_show)
    # Default subcommand is `discover` when none given
    args = p.parse_args(argv)
    if not args.cmd:
        args = p.parse_args(["discover", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
