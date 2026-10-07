#!/usr/bin/env python3
"""One-off: import the CMO's unshipped research drafts and verified business
candidates into this town's DB for operator review. Logic: engine/cmo_import.py.

Restore the CMO databases from restic snapshot 71b37ab2 first:

  set -a; . ~/.config/restic/env; set +a
  restic restore 71b37ab2 --target /tmp/cmo-jul31 \\
    --include /home/aaron/.hermes/skills/business/townloops-research/data \\
    --include /home/aaron/.hermes/skills/business/townloops-business-discovery/data
  B=/tmp/cmo-jul31/home/aaron/.hermes/skills/business

  PYTHONPATH=. python scripts/import_cmo.py \\
    --drafts $B/townloops-research/data/drafts.db \\
    --candidates $B/townloops-business-discovery/data/candidates.db [--dry-run]

Reads TownLoops (live dedup, after verify_town()); never writes to it. The CMO
data is South Lake Tahoe's, so it refuses any other TOWNLOOPS_TOWN_SLUG.
Applies pending migrations, imports in one transaction (rolled back with
--dry-run) and prints the report as JSON. Safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from engine import cmo_import
from engine import db as db_mod
from engine.derive_helpers import open_db

CMO_TOWN = "slt"


def main() -> int:
    ap = argparse.ArgumentParser(prog="import_cmo")
    ap.add_argument("--drafts", type=Path, required=True, help="CMO drafts.db")
    ap.add_argument("--candidates", type=Path, required=True, help="CMO candidates.db")
    ap.add_argument("--dry-run", action="store_true", help="report only; roll the import back")
    args = ap.parse_args()

    town = os.environ.get("TOWNLOOPS_TOWN_SLUG", "")
    if town != CMO_TOWN:
        print(f"the CMO data is {CMO_TOWN!r}'s; TOWNLOOPS_TOWN_SLUG is {town!r}", file=sys.stderr)
        return 1
    drafts = cmo_import.load_drafts(args.drafts)
    candidates = cmo_import.load_candidates(args.candidates)

    from engine.townloops_api import Client
    client = Client()
    client.verify_town()

    conn = open_db()
    db_mod.run_migrations(conn)
    conn.execute("BEGIN")
    try:
        report = cmo_import.run_import(conn, client, town, drafts=drafts, candidates=candidates)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if args.dry_run else "COMMIT")
    report["dry_run"] = args.dry_run
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
