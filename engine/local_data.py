"""The agent's own local stores (#64 "Local data ownership"): the agent may
read and change its SQLite databases directly. This module gives it what
makes that safe and visible, without restricting it:

- `schema_markdown` — every table and column of a migrated townloops.db,
  with the migrations' own column comments; committed as the local-data
  skill's `references/schema.md`, and a test fails when it drifts from
  the migrations.
- `log_edit` — one `agent_activity` row (action `local-edit`) with the
  one-line reason for a direct edit. Expected, not enforced.
- `backup` — SQLite's online backup of each state DB into
  `state/backups/<db>-<stamp>.db`, the last `KEEP` per DB kept. The
  analyst run's budget `start` takes one first (engine/analyst_runs).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engine import db as _db
from engine.derive_helpers import iso_utc_now

KEEP = 7
EDIT_ACTION = "local-edit"
# Other SQLite stores that may sit beside townloops.db (older towns).
EXTRA_DBS = ("discovery.db",)

_COMMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+[^-]*?--\s*(.+?)\s*$")


def schema_markdown(conn: sqlite3.Connection) -> str:
    """The schema reference of a migrated DB, as markdown (deterministic)."""
    conn.row_factory = sqlite3.Row
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    lines = [
        "# Local data — schema reference",
        "",
        "Generated from `engine/migrations/` by `local_data.py schema` — never edit by hand;",
        "`tests/test_town_report.py` fails when it no longer matches the migrations.",
        "",
        "The town's state DB is `$HERMES_HOME/state/townloops.db` (SQLite, WAL). Timestamps",
        "are ISO-8601 UTC text unless a column says otherwise; `*_json` / `extra_json` columns",
        "hold JSON text. Log a direct edit with `local_data.py log --reason \"…\"`.",
        "A note is the comment the table's first migration gave the column; later",
        "migrations may have widened it (e.g. more status values) — `engine/migrations/` says.",
        "",
        "Tables: " + ", ".join(f"`{t}`" for t in tables),
    ]
    for t in tables:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                           (t,)).fetchone()["sql"] or ""
        notes = {}
        for line in sql.splitlines():
            m = _COMMENT.match(line)
            if m:
                notes.setdefault(m[1], m[2])
        lines += ["", f"## {t}", "",
                  "| column | type | not null | default | pk | note |",
                  "|---|---|---|---|---|---|"]
        for c in conn.execute(f"PRAGMA table_info('{t}')"):
            lines.append("| " + " | ".join(_cell(v) for v in (
                c["name"], c["type"], "yes" if c["notnull"] else "", c["dflt_value"],
                c["pk"] or "", notes.get(c["name"]))) + " |")
        idx = [r for r in conn.execute(f"PRAGMA index_list('{t}')")]
        for i in sorted(idx, key=lambda r: r["name"]):
            cols = [r["name"] for r in conn.execute(f"PRAGMA index_info('{i['name']}')")]
            lines.append(f"\n{'Unique' if i['unique'] else 'Index'} `{i['name']}`: "
                         f"({', '.join(cols)})")
    return "\n".join(lines) + "\n"


def _cell(v) -> str:
    return "" if v is None else str(v).replace("|", "\\|")


def log_edit(conn: sqlite3.Connection, reason: str, *, table: Optional[str] = None,
             rows: Optional[int] = None, worker: str = "analyst") -> None:
    """Record a direct local edit and why (commits)."""
    detail = reason.strip() + (f" ({rows} rows)" if rows is not None else "")
    conn.execute("INSERT INTO agent_activity (ts, worker, action, target, detail, ok) "
                 "VALUES (?, ?, ?, ?, ?, 1)",
                 (iso_utc_now(), worker, EDIT_ACTION, table or "", detail[:400]))
    conn.commit()


def backup(db_path: Optional[str] = None, *, stamp: Optional[str] = None,
           keep: int = KEEP) -> list[str]:
    """Back up townloops.db (and any EXTRA_DBS beside it) into
    `<state>/backups/`; returns the files written, oldest pruned."""
    main = Path(db_path or _db.db_path_from_env())
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = main.parent / "backups"
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for src in [main, *(main.parent / n for n in EXTRA_DBS)]:
        if not src.exists():
            continue
        target = dest / f"{src.stem}-{stamp}.db"
        s, d = sqlite3.connect(src), sqlite3.connect(target)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        out.append(str(target))
        for old in sorted(dest.glob(f"{src.stem}-*.db"))[:-keep]:
            old.unlink(missing_ok=True)
    return out
