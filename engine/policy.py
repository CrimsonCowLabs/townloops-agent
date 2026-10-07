"""Safety rails for the autonomous onboarding loop.

Everything here is deliberately dumb & fast: pure functions plus a token
bucket. All I/O is either against the local SQLite (`post_counters` table
from migration 008) or the categories snapshot / TownLoops health endpoint.

Called from:
  - engine/orchestrator.py — reserve_post_slot() before every ship
  - engine/discovery.py    — firecrawl_bucket.wait() before every Firecrawl call
  - skills/business/*      — has_category() before enqueueing kind-specific work
  - engine/orchestrator.py — api_healthy() gates the shipping phase
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from engine.ingest_core import DEFAULT_TZ, _now_pt as _now_tz


# --------------------------------------------------------------------------
# Daily POST caps + onboarding ramp
# --------------------------------------------------------------------------

# Steady-state caps per (resource, day). Events left unbounded — event ingest
# self-limits by source cadence.
STEADY_CAPS: dict[str, int] = {
    "businesses":  30,
    "venues":      15,
    "communities": 15,
    "performers":  15,
    "creators":    15,
}


def caps_for_day(days_since_start: int) -> dict[str, int]:
    """Ramped caps: day 0 -> 5, day 1 -> 15, day 2+ -> STEADY_CAPS.

    days_since_start is measured from agent_state.entered_at of the very
    first phase (bootstrapping). Callers pass 0 for the first calendar day.
    """
    if days_since_start <= 0:
        return {r: 5 for r in STEADY_CAPS}
    if days_since_start == 1:
        return {r: 15 for r in STEADY_CAPS}
    return dict(STEADY_CAPS)


def today_str() -> str:
    return _now_tz().strftime("%Y-%m-%d")


def reserve_post_slot(
    conn: sqlite3.Connection,
    resource: str,
    days_since_start: int,
    *,
    day: Optional[str] = None,
) -> tuple[bool, str]:
    """Transactionally reserve one POST slot for (day, resource).

    Returns (True, "") if the reservation succeeded; (False, reason)
    otherwise. On True, the counter is already incremented — the caller MUST
    proceed with the POST or eat a wasted slot (we don't refund; a wasted
    slot per day is cheap insurance against the alternative).

    Events bypass the cap (returns True immediately) — event ingest is
    self-limiting per source and has no discovery-driven amplification.
    """
    if resource == "events":
        return (True, "")

    caps = caps_for_day(days_since_start)
    if resource not in caps:
        return (False, f"unknown resource {resource!r}")

    day = day or today_str()
    max_today = caps[resource]

    # Serializable-ish: SQLite single-writer + IMMEDIATE keeps other writers
    # blocked while we read-then-write the counter.
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "SELECT count FROM post_counters WHERE day = ? AND resource = ?",
            (day, resource),
        )
        row = cur.fetchone()
        current = row[0] if row else 0
        if current >= max_today:
            conn.execute("ROLLBACK")
            return (False, f"daily cap {max_today} reached for {resource}")
        if row:
            conn.execute(
                "UPDATE post_counters SET count = count + 1 WHERE day = ? AND resource = ?",
                (day, resource),
            )
        else:
            conn.execute(
                "INSERT INTO post_counters (day, resource, count) VALUES (?, ?, 1)",
                (day, resource),
            )
        conn.execute("COMMIT")
        return (True, "")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


def days_since(entered_at_iso: str) -> int:
    """Whole calendar days between entered_at and now, in AGENT_TZ.

    entered_at_iso should be an ISO-8601 string. Accepts both naive and
    zone-aware; naive is treated as AGENT_TZ.
    """
    try:
        started = datetime.fromisoformat(entered_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return 999  # unparseable -> treat as steady state (no ramp restriction)
    if started.tzinfo is None:
        started = started.replace(tzinfo=DEFAULT_TZ)
    now = _now_tz()
    return max(0, (now.date() - started.date()).days)


# --------------------------------------------------------------------------
# Firecrawl QPS token bucket
# --------------------------------------------------------------------------

@dataclass
class TokenBucket:
    """Simple monotonic token bucket. Thread-safe."""
    rate: float          # tokens per second refill
    burst: int           # max tokens
    _tokens: float = 0.0
    _last: float = 0.0
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._tokens = float(self.burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def try_take(self, n: int = 1) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def wait(self, n: int = 1, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if self.try_take(n):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"token bucket wait exceeded {timeout}s")
            time.sleep(min(0.5, max(0.05, n / max(self.rate, 0.1))))

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate)
            self._last = now


# Module-level singleton. Callers import `firecrawl_bucket` and .wait() before
# each Firecrawl call. Rate matches the "1 rps sustained, burst 10" agreement.
firecrawl_bucket = TokenBucket(rate=1.0, burst=10)


# --------------------------------------------------------------------------
# TownLoops API health gate
# --------------------------------------------------------------------------

def api_healthy(client) -> bool:
    """True iff the API healthz endpoint returns something non-empty w/o error.

    Used by the shipping phase — if unhealthy, skip and let the next tick
    retry. Never raises.
    """
    try:
        client.healthz()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Category-exists guard
# --------------------------------------------------------------------------

def categories_snapshot_path() -> Path:
    return Path(
        os.environ.get(
            "TOWNLOOPS_CATEGORIES_SNAPSHOT",
            os.path.join(
                os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
                "state",
                "categories.json",
            ),
        )
    )


def load_category_slugs(snapshot_path: Optional[Path] = None) -> set[str]:
    """Return the set of category slugs in the town's snapshot.

    Empty set on missing / malformed file (caller decides how to react).
    """
    path = snapshot_path or categories_snapshot_path()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    slugs: set[str] = set()
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("slug"), str):
                slugs.add(item["slug"])
    elif isinstance(data, dict):
        for item in data.get("categories") or []:
            if isinstance(item, dict) and isinstance(item.get("slug"), str):
                slugs.add(item["slug"])
    return slugs


def has_category(slug: str, snapshot_path: Optional[Path] = None) -> bool:
    return slug in load_category_slugs(snapshot_path)


# --------------------------------------------------------------------------
# Confidence-threshold gate for auto-promotion
# --------------------------------------------------------------------------

def is_high_confidence(verify_json: dict, town_name: str) -> tuple[bool, str]:
    """Decide whether a verified candidate is safe to auto-promote.

    Rules (all must hold):
      1. verify_json["verified"] is truthy
      2. verify_json has at least one of: address, phone, hours
      3. no closure marker in verify_json["reasons"] / notes
      4. town name substring (case-insensitive) appears in the scraped text
         signal (verify_json["evidence"] or verify_json["text"])

    Returns (ok, reason). reason is populated when ok is False so the
    orchestrator can log why a candidate was skipped.
    """
    if not isinstance(verify_json, dict):
        return (False, "verify_json missing")

    if not verify_json.get("verified"):
        return (False, "verify_json.verified is false")

    has_signal = any(bool(verify_json.get(k)) for k in ("address", "phone", "hours"))
    if not has_signal:
        return (False, "no address/phone/hours signal")

    text_blobs = " ".join(
        str(v) for k, v in verify_json.items()
        if k in ("reasons", "notes", "evidence", "text", "markdown_head")
        and v
    ).lower()

    for marker in ("permanently closed", "for lease", "out of business",
                   "closed permanently", "space available"):
        if marker in text_blobs:
            return (False, f"closure marker: {marker!r}")

    tn = (town_name or "").strip().lower()
    if tn and tn not in text_blobs:
        return (False, f"town name {tn!r} not present in evidence")

    return (True, "")
