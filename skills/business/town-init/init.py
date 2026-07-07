#!/usr/bin/env python3
"""town-init — bootstrap the town on first boot / a fresh clone.

Subcommands:
  setup   verify credentials, snapshot categories, migrate the state DB.
  status  dashboard of counts + enrichment queue depth + last newsletter.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")  # docker mount
from engine import townloops_api, listmonk_api, db  # noqa: E402


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
STATE_DIR = HERMES_HOME / "state"
CATEGORIES_PATH = STATE_DIR / "categories.json"


def _log(msg: str) -> None:
    sys.stderr.write(f"[town-init] {msg}\n")


def cmd_setup(args) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # 1. TownLoops auth.
    tl = townloops_api.Client()
    _log(f"town={tl.town_slug}  api={tl.base_url}")
    try:
        tl.healthz()
    except Exception as e:
        _log(f"FATAL: /healthz failed: {e}")
        return 2
    try:
        # Cheap read to confirm the key works for this town.
        tl.list("venues", limit=1)
    except townloops_api.TownloopsError as e:
        _log(f"FATAL: bearer token rejected for town {tl.town_slug}: {e}")
        return 2
    _log("townloops auth ok")

    # 2. Listmonk auth (best-effort — non-fatal if the newsletter isn't set up yet).
    if os.environ.get("LISTMONK_URL"):
        try:
            lm = listmonk_api.Client()
            lists = lm.lists()
            _log(f"listmonk auth ok ({len(lists)} lists visible)")
        except Exception as e:
            _log(f"WARN: listmonk unreachable: {e}")
    else:
        _log("skip listmonk (LISTMONK_URL unset)")

    # 3. Category snapshot.
    try:
        categories = tl.list("categories", limit=200)
        CATEGORIES_PATH.write_text(json.dumps(categories, indent=2))
        _log(f"snapshot {len(categories)} categories → {CATEGORIES_PATH}")
    except Exception as e:
        _log(f"WARN: categories snapshot failed: {e}")

    # 4. State DB migrations.
    conn = db.connect()
    applied = db.run_migrations(conn)
    if applied:
        _log(f"applied migrations: {', '.join(applied)}")
    else:
        _log("state DB already up to date")
    conn.close()

    print(
        json.dumps(
            {
                "town_slug": tl.town_slug,
                "state_dir": str(STATE_DIR),
                "categories": str(CATEGORIES_PATH),
                "applied_migrations": applied,
                "ok": True,
            },
            indent=2,
        )
    )
    return 0


def cmd_status(args) -> int:
    tl = townloops_api.Client()
    counts = {}
    for kind in ("events", "venues", "businesses", "communities", "performers", "creators"):
        try:
            counts[kind] = len(tl.list(kind, limit=1)) and "≥1" or 0
            # Follow up with a real pagination count.
            counts[kind] = sum(1 for _ in tl.paginate(kind))
        except Exception as e:
            counts[kind] = f"error: {e}"

    # Local queue depth.
    conn = db.connect()
    q_depth = {}
    try:
        for kind in ("venue", "business", "performer"):
            row = conn.execute(
                "SELECT COUNT(*) c FROM enrichment_queue "
                "WHERE kind=? AND claimed_at IS NULL AND resolved_at IS NULL",
                (kind,),
            ).fetchone()
            q_depth[kind] = row["c"] if row else 0
    except Exception:
        q_depth = {"error": "enrichment_queue not migrated yet"}

    # Last newsletter.
    try:
        row = conn.execute(
            "SELECT iso_week, status, scheduled_send_at "
            "FROM newsletters ORDER BY iso_week DESC LIMIT 1"
        ).fetchone()
        last_newsletter = dict(row) if row else None
    except Exception:
        last_newsletter = None
    conn.close()

    print(
        json.dumps(
            {
                "town_slug": tl.town_slug,
                "counts": counts,
                "enrichment_queue": q_depth,
                "last_newsletter": last_newsletter,
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(func=cmd_setup)
    sub.add_parser("status").set_defaults(func=cmd_status)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
