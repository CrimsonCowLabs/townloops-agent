#!/usr/bin/env python3
"""entity-research — resolve category_slug for entity rows the discovery
skills couldn't auto-assign (keyword-map was ambiguous or missed).

Two subcommands:

  classify  — for a target kind (business/community/venue/…), find rows
              with category_slug IS NULL and try to classify them. Uses
              engine.policy.resolve_category_slug first; if the map returns
              None AND the Codex CLI is available, shells out to
              `codex exec` with a small prompt returning a single slug.
              If no LLM route is available, leaves the row unclassified
              (ship phase will report the failure — not a blocker for
              the tick).
  list      — read-only inspection of currently unclassified rows.

LLM auth flows through the Codex CLI's ChatGPT session (~/.codex/auth.json,
mounted rw into the container so token refresh survives). Rate-limit or
transient errors write state/llm_health.json and return None — the hourly
onboarding-tick is the outer retry loop, matching the hours-scale ChatGPT
plan backoff window.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engine import db as _db
from engine import policy


_CLASSIFY_TABLES = {
    "business":  "businesses",
    "community": "communities",
    "venue":     "venues",
    "performer": "performers",
    "creator":   "creators",
}

# Session-refresh + real 429s from the ChatGPT backend surface as stderr
# text; keep the pattern intentionally loose so we don't miss a rename.
_RATE_LIMIT_RE = re.compile(
    r"rate.?limit|429|quota|throttl|too many requests|usage limit",
    re.IGNORECASE,
)

_HERMES_HOME = os.environ.get("HERMES_HOME", "/home/agent/.hermes")
_LLM_HEALTH_PATH = Path(_HERMES_HOME) / "state" / "llm_health.json"


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_health(status: str, **fields) -> None:
    """Persist LLM-call health so the orchestrator's tick-top hook can
    detect healthy↔throttled transitions and Telegram-notify once per flip.
    """
    _LLM_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    prev: dict = {}
    if _LLM_HEALTH_PATH.exists():
        try:
            prev = json.loads(_LLM_HEALTH_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            prev = {}
    # Preserve last-notified state so the notifier can dedupe.
    payload = {
        "status": status,
        "checked_at": _iso_utc_now(),
        "last_notified_status": prev.get("last_notified_status"),
        "last_notified_at": prev.get("last_notified_at"),
    }
    payload.update(fields)
    try:
        _LLM_HEALTH_PATH.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        print(f"[entity-research][llm-health-write-failed] {exc}",
              file=sys.stderr)


def _build_prompt(name: str, description: str, url: str,
                  available_slugs: list[str]) -> str:
    return (
        "You are a taxonomy classifier for a local-town directory. "
        "Return the single best-fit category slug from the provided list. "
        "Reply with just the slug on one line — no explanation, no markdown.\n\n"
        f"Categories: {', '.join(available_slugs)}\n\n"
        f"Name: {name}\n"
        f"Description: {description[:400]}\n"
        f"URL: {url}\n\n"
        "Slug:"
    )


def _parse_slug(stdout: str, available_slugs: set[str]) -> Optional[str]:
    """Codex CLI stdout can include agent noise before the final answer.
    Scan lines bottom-up; return the first token that matches an allowed
    slug (case-insensitive)."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        # Strip common prefixes/wrappers.
        for prefix in ("slug:", "answer:", "-", "*", "•", "`", "\""):
            if line.lower().startswith(prefix):
                line = line[len(prefix):].strip()
        token = line.split()[0].rstrip(".,;:\"`").lower() if line.split() else ""
        if token in available_slugs:
            return token
    return None


def _llm_classify(name: str, description: str, url: str,
                  available_slugs: list[str]) -> Optional[str]:
    """Codex-CLI classifier. Returns None if the CLI is missing, the call
    errors, the response can't be parsed to an allowed slug, or the CLI
    reports throttling. Retries once on transient network errors.

    We deliberately keep the prompt small; this is a one-shot classifier,
    not a chat.
    """
    if not available_slugs:
        return None
    if not shutil.which("codex"):
        _write_health("unavailable", last_error="codex CLI not on PATH")
        return None

    prompt = _build_prompt(name, description, url, available_slugs)
    # Codex CLI v0.145+: `codex exec [OPTIONS] [PROMPT]`. Options:
    #   -m MODEL              (override default; omit to let CLI pick per plan)
    #   --skip-git-repo-check (container FS is not a git repo)
    #   --sandbox read-only   (classifier must not execute shell code)
    #   --ephemeral           (no session files written to ~/.codex)
    cmd = ["codex", "exec",
           "--skip-git-repo-check",
           "--sandbox", "read-only",
           "--ephemeral"]
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if model:
        cmd += ["-m", model]
    cmd.append(prompt)

    allowed = {s.lower() for s in available_slugs}

    for attempt in (1, 2):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            if attempt == 1:
                time.sleep(30)
                continue
            _write_health("throttled",
                          last_error_at=_iso_utc_now(),
                          last_error="codex exec timed out (60s) x2")
            return None
        except OSError as exc:
            _write_health("throttled",
                          last_error_at=_iso_utc_now(),
                          last_error=f"codex exec OS error: {exc}")
            return None

        stderr_tail = (proc.stderr or "").strip()[-500:]

        if proc.returncode == 0:
            slug = _parse_slug(proc.stdout, allowed)
            if slug is not None:
                _write_health("healthy", last_ok_at=_iso_utc_now())
                return slug
            # Success return code but unparseable — model returned junk.
            # Don't mark throttled; call it healthy and move on.
            _write_health("healthy", last_ok_at=_iso_utc_now(),
                          last_parse_miss=(proc.stdout or "").strip()[-200:])
            return None

        # Non-zero exit. Classify the failure.
        if _RATE_LIMIT_RE.search(stderr_tail):
            _write_health("throttled",
                          last_error_at=_iso_utc_now(),
                          last_error=stderr_tail[:200])
            print(f"[entity-research][llm-throttled] {stderr_tail[:200]}",
                  file=sys.stderr)
            return None

        # Transient? one retry with a short backoff.
        if attempt == 1:
            time.sleep(30)
            continue
        _write_health("throttled",
                      last_error_at=_iso_utc_now(),
                      last_error=stderr_tail[:200] or f"codex exit {proc.returncode}")
        print(f"[entity-research][llm-error rc={proc.returncode}] {stderr_tail[:200]}",
              file=sys.stderr)
        return None

    return None


def cmd_classify(args) -> int:
    table = _CLASSIFY_TABLES.get(args.kind)
    if not table:
        print(f"unknown kind {args.kind!r}", file=sys.stderr)
        return 2

    conn = _db.connect()
    conn.row_factory = sqlite3.Row

    available = list(policy.load_category_slugs())
    if not available:
        print("[entity-research] no categories snapshot — refusing to classify",
              file=sys.stderr)
        return 3

    rows = conn.execute(
        f"""
        SELECT entity_uid, name, description, source_url
        FROM {table}
        WHERE category_slug IS NULL OR category_slug = ''
        ORDER BY first_seen_at
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()

    map_hits = 0
    llm_hits = 0
    unclassified = 0
    for r in rows:
        name = r["name"] or ""
        desc = r["description"] or ""
        # 1. Keyword map
        slug = policy.resolve_category_slug(
            args.kind, name, desc, set(available),
        )
        if slug:
            conn.execute(
                f"UPDATE {table} SET category_slug = ? WHERE entity_uid = ?",
                (slug, r["entity_uid"]),
            )
            map_hits += 1
            continue
        # 2. LLM fallback
        slug = _llm_classify(name, desc, r["source_url"] or "", available)
        if slug:
            conn.execute(
                f"UPDATE {table} SET category_slug = ? WHERE entity_uid = ?",
                (slug, r["entity_uid"]),
            )
            llm_hits += 1
        else:
            unclassified += 1
    conn.commit()

    print(json.dumps({
        "kind": args.kind, "table": table,
        "processed": len(rows),
        "map_hits": map_hits, "llm_hits": llm_hits,
        "still_unclassified": unclassified,
    }))
    return 0


def cmd_list(args) -> int:
    table = _CLASSIFY_TABLES.get(args.kind)
    if not table:
        print(f"unknown kind {args.kind!r}", file=sys.stderr)
        return 2
    conn = _db.connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT entity_uid, name, source_url
        FROM {table}
        WHERE category_slug IS NULL OR category_slug = ''
        ORDER BY first_seen_at
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    for r in rows:
        print(json.dumps(dict(r), ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="entity-research")
    sub = p.add_subparsers(dest="cmd", required=True)

    cl = sub.add_parser("classify", help="Fill category_slug on rows that lack it")
    cl.add_argument("--kind", type=str, required=True,
                    choices=list(_CLASSIFY_TABLES.keys()))
    cl.add_argument("--limit", type=int, default=10)

    ls = sub.add_parser("list", help="List rows lacking category_slug")
    ls.add_argument("--kind", type=str, required=True,
                    choices=list(_CLASSIFY_TABLES.keys()))
    ls.add_argument("--limit", type=int, default=50)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return {"classify": cmd_classify, "list": cmd_list}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
