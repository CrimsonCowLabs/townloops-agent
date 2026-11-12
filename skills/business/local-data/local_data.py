#!/usr/bin/env python3
"""local-data — the agent's own SQLite stores (#64): their schema reference,
the log line for a direct edit, and backups. Logic: engine/local_data.py.

Subcommands:
  schema [--out PATH]
        render every table/column of a freshly migrated townloops.db as
        markdown (stdout, or PATH — the committed copy is references/schema.md)
  log --reason "…" [--table T] [--rows N]
        record a direct local edit and why (agent_activity, action local-edit)
  backup
        online backup of the state DBs into state/backups/ (last 7 kept)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from engine import db, local_data
from engine.derive_helpers import open_db


def _schema(args) -> int:
    conn = sqlite3.connect(":memory:")
    db.run_migrations(conn)
    text = local_data.schema_markdown(conn)
    if args.out:
        Path(args.out).write_text(text)
        print(json.dumps({"written": args.out}))
    else:
        sys.stdout.write(text)
    return 0


def _log(args) -> int:
    local_data.log_edit(open_db(), args.reason, table=args.table, rows=args.rows)
    print(json.dumps({"logged": args.reason}))
    return 0


def _backup(args) -> int:
    print(json.dumps({"backups": local_data.backup()}))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="local-data")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("schema")
    s.add_argument("--out")
    lg = sub.add_parser("log")
    lg.add_argument("--reason", required=True)
    lg.add_argument("--table")
    lg.add_argument("--rows", type=int)
    sub.add_parser("backup")
    args = ap.parse_args(argv)
    return {"schema": _schema, "log": _log, "backup": _backup}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
