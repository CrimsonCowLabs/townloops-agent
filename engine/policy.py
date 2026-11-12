"""Safety rails shared by the pipeline.

Everything here is deliberately dumb & fast: pure functions plus a token
bucket. All I/O is against the categories snapshot or the TownLoops health
endpoint. There are no per-day POST caps: creates are bounded by the
workqueue's hourly rate limits (engine/workqueue) and, in autonomous mode, by
the submission gate's pending cap (engine/autonomy.GatedClient). Migration
008's `post_counters` table is unused.

Called from:
  - engine/discovery.py, research.py, event_extract.py — firecrawl_bucket.wait()
    before every Firecrawl call
  - engine/preflight.py    — has_category() on the profile's query groups
  - engine/discovery.py    — is_high_confidence() before auto-promotion
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


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
