"""SQLite connection + migrations runner for the town's state DB."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = os.environ.get(
    "TOWNLOOPS_DB_PATH", "/home/agent/.hermes/state/townloops.db"
)
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(path: str | None = None) -> sqlite3.Connection:
    p = Path(path or DEFAULT_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def run_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply any *.sql files under engine/migrations/ that haven't run yet.

    Returns the list of filenames applied this call.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
          filename    TEXT PRIMARY KEY,
          applied_at  TEXT NOT NULL
        )
        """
    )
    applied = {
        row["filename"]
        for row in conn.execute("SELECT filename FROM schema_version").fetchall()
    }
    new_files: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        conn.executescript(path.read_text())
        conn.execute(
            "INSERT INTO schema_version(filename, applied_at) VALUES (?, ?)",
            (path.name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
        new_files.append(path.name)
    return new_files
