"""Shared helpers for the event-derive-* skills (M3).

Skill contract with orchestrator._invoke_skill:
- Invoked as: python <entry>.py --event-uid <uid>
- Return 0 on success (orchestrator advances state)
- Return non-zero on failure (orchestrator retries up to 3, then stuck)
- Skills should NOT advance events.state themselves — orchestrator owns
  the state machine.

The helpers here centralize:
- CLI arg parsing (every skill takes just --event-uid)
- DB open with WAL + row_factory
- Event loading
- Normalized-name matching (venue dedup, host dedup)
- entity_uid minting
- linked_events insert
- Activity recording
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_DB_PATH = Path(os.environ.get(
    "TOWNLOOPS_DB_PATH",
    os.path.join(
        os.environ.get("HERMES_HOME", "/home/agent/.hermes"),
        "state", "townloops.db",
    ),
))


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def parse_event_uid_arg(argv: Optional[list[str]] = None) -> str:
    p = argparse.ArgumentParser()
    p.add_argument("--event-uid", required=True)
    args = p.parse_args(argv)
    return args.event_uid


def load_event(conn: sqlite3.Connection, event_uid: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM events WHERE event_uid = ?", (event_uid,),
    ).fetchone()
    return dict(row) if row else None


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def normalize_name(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Used for
    dedup lookups: `Kings Beach Community Ctr.` == `kings beach community ctr`."""
    s = (s or "").strip().lower()
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return s


def mint_entity_uid(kind: str, seed_name: str, event_uid: str) -> str:
    """Deterministic entity_uid so re-derivation of the same event doesn't
    spawn duplicate entities. `event_uid` is included so different events
    at the same venue still produce ONE venue row (the normalize_name
    lookup catches the collision before we ever call this)."""
    key = f"{kind}::{normalize_name(seed_name)}::{event_uid}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def link_event_to_entity(conn: sqlite3.Connection, event_uid: str,
                         entity_kind: str, entity_uid: str,
                         role: str = "primary") -> None:
    """Idempotent link. Increments the entity's linked_event_count cache
    ONLY if this is a new link (SQLite doesn't have UPSERT RETURNING, so
    we check first)."""
    existing = conn.execute(
        "SELECT 1 FROM linked_events WHERE event_uid = ? AND entity_kind = ? "
        "AND entity_uid = ? AND role = ?",
        (event_uid, entity_kind, entity_uid, role),
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO linked_events(event_uid, entity_kind, entity_uid, role, linked_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_uid, entity_kind, entity_uid, role, iso_utc_now()),
    )
    table = _entity_table(entity_kind)
    if table:
        conn.execute(
            f"UPDATE {table} SET linked_event_count = COALESCE(linked_event_count, 0) + 1 "
            f"WHERE entity_uid = ?", (entity_uid,),
        )


def _entity_table(kind: str) -> Optional[str]:
    return {
        "venue": "venues",
        "business": "businesses",
        "community": "communities",
        "performer": "performers",
        "creator": "creators",
    }.get(kind)


def record_activity(conn: sqlite3.Connection, worker: str, action: str,
                    target: str, detail: str = "", ok: bool = True,
                    error: Optional[str] = None) -> None:
    """Write one row to agent_activity so the dashboard's live band shows
    what M3 skills are doing. Kept lightweight — skills call this often."""
    conn.execute(
        "INSERT INTO agent_activity(ts, worker, action, target, detail, "
        "  duration_ms, ok, error) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
        (iso_utc_now(), worker, action, target[:200], detail[:400],
         1 if ok else 0, error),
    )
