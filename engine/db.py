"""SQLite connection + migrations runner for the town's state DB."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Every other module in this repo derives state paths from $HERMES_HOME
# (see engine/lock.py, skills/business/event-ingest/ingest.py, town-init,
# event-source-discovery, newsletter-weekly). db.py used to hardcode
# /home/agent/.hermes/... which broke bare-systemd deploys where the agent
# home isn't `/home/agent/`. Fixed 2026-07-08 to match the pattern.
_HERMES_HOME = os.environ.get("HERMES_HOME", "/home/agent/.hermes")
DEFAULT_DB_PATH = os.environ.get(
    "TOWNLOOPS_DB_PATH",
    os.path.join(_HERMES_HOME, "state", "townloops.db"),
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


_LINE_COMMENT_RE = re.compile(r"^\s*--.*$", re.MULTILINE)


def _split_sql(text: str) -> list[str]:
    """Split a migration script into statements, ignoring `--` line comments and
    respecting single/double-quoted string literals.

    Sufficient for our DDL-only migrations. Does NOT support `BEGIN...END`
    trigger bodies — none exist in this repo. Add a bigger parser (or use
    sqlparse) before writing one.
    """
    text = _LINE_COMMENT_RE.sub("", text)
    stmts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == ";":
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _apply_one_migration(conn: sqlite3.Connection, path: Path) -> None:
    """Run one migration + record schema_version in a single transaction.

    On failure, rolls back — schema is unchanged AND schema_version is NOT
    written, so the next boot retries the same migration cleanly.

    executescript() would auto-commit before running, defeating atomicity;
    we split statements ourselves and drive BEGIN/COMMIT manually.
    """
    stmts = _split_sql(path.read_text())
    conn.execute("BEGIN")
    try:
        for stmt in stmts:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version(filename, applied_at) VALUES (?, ?)",
            (path.name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            # Already rolled back / no active txn — safe to ignore.
            pass
        raise


def run_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply any *.sql files under engine/migrations/ that haven't run yet.

    Each migration runs atomically — partial failures leave zero footprint.

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
    conn.commit()
    applied = {
        row["filename"]
        for row in conn.execute("SELECT filename FROM schema_version").fetchall()
    }
    new_files: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        _apply_one_migration(conn, path)
        new_files.append(path.name)
    return new_files
