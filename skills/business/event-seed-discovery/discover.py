#!/usr/bin/env python3
"""event-seed-discovery — LLM proposes seed source URLs; HEAD-verified; inserted as proposed.

Autonomous mode adds `review` (engine/autonomy): proposed seeds are dry-run
and approved only when the run yields at least one real upcoming in-town
event (an error, challenge or empty page fails), seeds with a feed first;
approved seeds are classified primary/syndicator from their events, their
trust refreshed; trust decides the rest — a trial must score
`seed_trust.approve`, a seed below `seed_trust.syndicator` is demoted to
syndicator and one below `seed_trust.retire` — or one dead for several runs
whose calendar can't be found again on its own site — is retired
(reversible: `restore`). A moved calendar replaces the seed URL."""

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

from engine import autonomy, event_dedup, feed_probe, inspection, source_health
from engine.event_extract import (
    build_extraction_prompt, firecrawl_markdown as _trial_fetch,
    parse_json_array as _parse_events, utc_times,
)


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
    q = "SELECT seed_id, name, url FROM event_seeds WHERE status NOT IN ('rejected', 'retired')"
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


# --------------------------------------------------------------------------
# Autonomous review
# --------------------------------------------------------------------------

def _site_search(query: str) -> list[dict]:
    from engine.discovery import firecrawl_search
    return firecrawl_search(query, limit=5)


def _set_status(conn, seed: dict, status: str, d: "autonomy.Decision") -> None:
    conn.execute("UPDATE event_seeds SET status = ?, status_note = ?, status_changed_at = ? "
                 "WHERE seed_id = ?", (status, f"[autonomy] {d.reason}", _iso_utc_now(),
                                       seed["seed_id"]))
    autonomy.record(conn, "seed", f"seed:{seed['name']}", d)
    conn.commit()


def _trial(conn, profile: dict, mode, name: str, url: str) -> "autonomy.Decision":
    """Dry-run one seed URL: scrape, extract, judge what it lists."""
    ok, md, err = _trial_fetch(url)
    if not ok:
        return autonomy.Decision("reject", "trial-unreachable", {"url": url, "error": err[:200]})
    failure = autonomy.page_failure(md)
    if failure:
        return autonomy.judge_trial([], [], failure)
    ok, out, err = _codex_exec(build_extraction_prompt(profile, name, url, md))
    if not ok:
        return autonomy.Decision("defer", "ai-unavailable", {"url": url})
    items = []
    for e in _parse_events(out):
        if not (isinstance(e, dict) and e.get("title") and e.get("date")):
            continue
        start, end = utc_times(e.get("date"), e.get("end_at"))
        items.append({"title": e["title"], "date": e["date"], "date_utc": start,
                      "end_at_utc": end, "venue": e.get("venue"), "location": e.get("location"),
                      "source_url": e.get("source_url")})
    if not items:
        return autonomy.judge_trial([], [], "empty-listing")
    ctx = autonomy.judge_context(conn, profile, mode)
    d = autonomy.judge_trial(items, autonomy.judge_items(items, md, ctx, _codex_exec), None, mode)
    d.evidence["url"] = url
    return d


def _find_moved(conn, profile: dict, mode, seed: dict) -> tuple[Optional[str], list[str]]:
    """A working calendar URL for a dead seed on its own site: where the seed
    page now redirects, else what a same-site search finds. Each candidate
    must pass the same trial as a proposed seed."""
    tried: list[str] = []
    candidates: list[str] = []
    ok, code, html, final_url = _scrape_page(seed["url"])
    if ok:
        _record_feed(conn, seed["url"], html, final_url)
        if final_url and final_url.rstrip("/") != seed["url"].rstrip("/"):
            candidates.append(final_url)
    host = (urlparse(seed["url"]).hostname or "").lower()
    if host:
        for r in _site_search(f"site:{host.removeprefix('www.')} events calendar"):
            u = (r.get("url") or "").strip()
            if u and (urlparse(u).hostname or "").lower().removeprefix("www.") == \
                    host.removeprefix("www.") and u.rstrip("/") != seed["url"].rstrip("/"):
                candidates.append(u)
    for url in list(dict.fromkeys(candidates))[:3]:
        tried.append(url)
        if conn.execute("SELECT 1 FROM event_seeds WHERE url = ? AND seed_id != ?",
                        (url, seed["seed_id"])).fetchone():
            continue
        if _trial(conn, profile, mode, seed["name"], url).verdict == "approve":
            return url, tried
    return None, tried


def cmd_review(args) -> int:
    profile = _load_town_profile()
    mode = autonomy.settings(profile)
    if not mode.autonomous:
        print(json.dumps(autonomy.inactive_message("event-seed-discovery review")))
        return 0
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    out = {"trialed": 0, "approved": 0, "rejected": 0, "deferred": 0, "roles": 0,
           "retired": 0, "relocated": 0}
    proposed = [dict(r) for r in conn.execute(
        "SELECT * FROM event_seeds WHERE status = 'proposed' "
        "ORDER BY feed_url IS NULL, trial_at IS NOT NULL, trial_at, seed_id LIMIT ?",
        (args.trials,))]
    for seed in proposed:
        out["trialed"] += 1
        d = _trial(conn, profile, mode, seed["name"], seed["url"])
        # The trial score is the trust a seed starts with (an approved seed's
        # is then earned from moderator outcomes).
        conn.execute("UPDATE event_seeds SET trial_at = ?, "
                     "trust_score = COALESCE(?, trust_score) WHERE seed_id = ?",
                     (_iso_utc_now(), d.evidence.get("trust"), seed["seed_id"]))
        if d.verdict == "approve":
            _set_status(conn, seed, "approved", d)
            out["approved"] += 1
        elif d.verdict == "reject":
            _set_status(conn, seed, "rejected", d)
            out["rejected"] += 1
        else:
            autonomy.record(conn, "seed", f"seed:{seed['name']}", d)
            out["deferred"] += 1
        conn.commit()

    autonomy.refresh_trust(conn)
    for seed in [dict(r) for r in conn.execute("SELECT * FROM event_seeds WHERE status = 'approved'")]:
        role = autonomy.classify_role(conn, seed, mode)
        if role.verdict in (event_dedup.PRIMARY, event_dedup.SYNDICATOR) \
                and seed.get("source_role") != role.verdict:
            conn.execute("UPDATE event_seeds SET source_role = ? WHERE seed_id = ?",
                         (role.verdict, seed["seed_id"]))
            autonomy.record(conn, "seed-role", f"seed:{seed['name']}", role)
            seed["source_role"] = role.verdict
            autonomy.refresh_trust(conn, [seed["name"]])   # a syndicator's dedup losses don't count
            out["roles"] += 1
        d = autonomy.judge_seed(conn, seed, mode)
        if d.verdict == "retire":
            _set_status(conn, seed, "retired", d)
            out["retired"] += 1
        elif d.verdict == "check-moved" and out["relocated"] + out["retired"] < args.moves:
            moved, tried = _find_moved(conn, profile, mode, seed)
            if moved:
                conn.execute(
                    "UPDATE event_seeds SET previous_url = url, url = ?, status_note = ?, "
                    "  status_changed_at = ?, health_status = NULL, health_note = NULL, "
                    "  health_baseline = NULL, health_flagged_at = NULL, health_since = ? "
                    "WHERE seed_id = ?",
                    (moved, f"[autonomy] calendar moved from {seed['url']}", _iso_utc_now(),
                     _iso_utc_now(), seed["seed_id"]))
                autonomy.record(conn, "seed", f"seed:{seed['name']}", autonomy.Decision(
                    "relocate", "calendar-moved", {**d.evidence, "from": seed["url"],
                                                   "to": moved}))
                conn.commit()
                out["relocated"] += 1
            else:
                _set_status(conn, seed, "retired", autonomy.Decision(
                    "retire", d.reason, {**d.evidence, "searched": tried}))
                out["retired"] += 1
    conn.commit()
    conn.close()
    inspection.record(worker="event-seed-discovery", action="skill-invoke", target="review",
                      detail=json.dumps(out), ok=True)
    print(json.dumps(out))
    return 0


def cmd_retire(args) -> int:
    """Operator: retire a seed by hand (reversible with `restore`)."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM event_seeds WHERE seed_id = ?", (args.seed_id,)).fetchone()
    if not row:
        print(json.dumps({"error": f"no seed {args.seed_id}"}))
        return 1
    _set_status(conn, dict(row), "retired",
                autonomy.Decision("retire", "operator", {"note": args.reason or ""}))
    print(json.dumps({"seed_id": args.seed_id, "status": "retired"}))
    return 0


def cmd_restore(args) -> int:
    """Undo a retirement (or a rejection): the seed is approved again, and
    with --previous-url its URL from before a calendar move comes back."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM event_seeds WHERE seed_id = ?", (args.seed_id,)).fetchone()
    if not row or row["status"] not in ("retired", "rejected") and not args.previous_url:
        print(json.dumps({"error": f"seed {args.seed_id} is not retired or rejected"}))
        return 1
    if args.previous_url and row["previous_url"]:
        conn.execute("UPDATE event_seeds SET url = previous_url, previous_url = url "
                     "WHERE seed_id = ?", (args.seed_id,))
    _set_status(conn, dict(row), "approved",
                autonomy.Decision("restore", "operator", {"was": row["status"]}))
    print(json.dumps({"seed_id": args.seed_id, "status": "approved"}))
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
    rv = sub.add_parser("review", help="autonomous: trial proposed seeds, classify, retire")
    rv.add_argument("--trials", type=int, default=2, help="proposed seeds dry-run this run")
    rv.add_argument("--moves", type=int, default=2, help="dead seeds searched for a moved calendar")
    rv.set_defaults(func=cmd_review)
    rt = sub.add_parser("retire", help="retire a seed (reversible)")
    rt.add_argument("--seed-id", type=int, required=True)
    rt.add_argument("--reason")
    rt.set_defaults(func=cmd_retire)
    rs = sub.add_parser("restore", help="approve a retired/rejected seed again")
    rs.add_argument("--seed-id", type=int, required=True)
    rs.add_argument("--previous-url", action="store_true",
                    help="also put back the URL from before a calendar move")
    rs.set_defaults(func=cmd_restore)
    # Default subcommand is `discover` when none given
    args = p.parse_args(argv)
    if not args.cmd:
        args = p.parse_args(["discover", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
