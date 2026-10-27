#!/usr/bin/env python3
"""egress-hosts — open (or close) an outbound host for this town's agent.

Writes $HERMES_HOME/egress/allowed_hosts.yaml (engine/egress.py: the format,
the rules, and the merge into the shared Squid allowlist). The host becomes
reachable only after bin/townloops-egress-sync on the host recompiles and
reloads Squid — typically within a few minutes.

Subcommands:
  add HOST --reason TEXT [--seed-id N] [--evidence URL]
                     validate with the compiler's own rules, add, record
  remove HOST        drop a host this agent added (never a profile host)
  list               the agent's hosts, and the profile's for comparison

Every change is an `agent_activity` row (worker egress-hosts), which the
nightly digest lists.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

SYNC_NOTE = ("reachable once the host-side sync (bin/townloops-egress-sync) recompiles and "
             "reloads Squid — typically within a few minutes; until then the proxy answers 403")
REMOVE_NOTE = ("blocked again once the host-side sync (bin/townloops-egress-sync) recompiles "
               "and reloads Squid — typically within a few minutes")


def _profile_hosts() -> list[str]:
    from engine.derive_helpers import load_town_profile
    hosts = load_town_profile().get("allowed_hosts") or []
    if not isinstance(hosts, list):
        return []
    return [str(h).strip().lower().lstrip(".") for h in hosts]


def _covering(host: str) -> Optional[str]:
    """The profile entry that already allows `host` (it or a parent domain)."""
    return next((p for p in _profile_hosts() if host == p or host.endswith("." + p)), None)


def _record(action: str, entry: dict) -> None:
    """One agent_activity row; the reason is cut short so the row (≤400
    chars) stays JSON — the file keeps the whole entry."""
    from engine import db, egress
    from engine.derive_helpers import record_activity
    detail = {"reason": str(entry.get("reason") or "")[:200], "seed_id": entry.get("seed_id")}
    conn = db.connect(db.db_path_from_env())
    try:
        record_activity(conn, egress.AGENT_WORKER, action, str(entry.get("host")),
                        detail=json.dumps(detail, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()


def _out(payload: dict, rc: int = 0) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return rc


def _add(args, slug: str) -> int:
    from engine import egress
    from engine.derive_helpers import iso_utc_now
    try:
        host = egress.check_host(slug, args.host)
        covered = _covering(host)
        if covered:
            return _out({"added": False, "host": host,
                         "note": f"already allowed by town-profile.yaml allowed_hosts ({covered})"})
        entry, added = egress.add_agent_host(
            egress.agent_hosts_path(), slug, host, reason=args.reason, now=iso_utc_now(),
            seed_id=args.seed_id, evidence=args.evidence)
    except egress.EgressError as e:
        return _out({"error": str(e)}, 1)
    if added:
        _record("add", entry)
    return _out({"added": added, "entry": entry,
                 "note": SYNC_NOTE if added else "already added — " + SYNC_NOTE})


def _remove(args, slug: str) -> int:
    from engine import egress
    host = str(args.host).strip().lower().lstrip(".")
    try:
        gone = egress.remove_agent_host(egress.agent_hosts_path(), slug, host)
    except egress.EgressError as e:
        return _out({"error": str(e)}, 1)
    if gone is None:
        covered = _covering(host)
        why = (f"it is allowed by town-profile.yaml ({covered}) — the operator's list, not mine"
               if covered else "not one of this agent's hosts")
        return _out({"error": f"{host}: {why}"}, 1)
    _record("remove", gone)
    return _out({"removed": gone, "note": REMOVE_NOTE})


def _list(args, slug: str) -> int:
    from engine import egress
    try:
        entries = egress.read_agent_hosts(egress.agent_hosts_path())
    except egress.EgressError as e:
        return _out({"error": str(e)}, 1)
    return _out({"town": slug, "file": str(egress.agent_hosts_path()),
                 "agent_hosts": entries, "profile_hosts": _profile_hosts()})


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="egress-hosts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("host")
    add.add_argument("--reason", required=True, help="why this town needs it")
    add.add_argument("--seed-id", type=int, default=None, help="the seed it serves")
    add.add_argument("--evidence", default=None, help="URL showing the need (a feed, an API call)")
    rm = sub.add_parser("remove")
    rm.add_argument("host")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    slug = os.environ.get("TOWNLOOPS_TOWN_SLUG", "")
    if not slug:
        return _out({"error": "TOWNLOOPS_TOWN_SLUG unset"}, 2)
    from engine import db
    db.migrate()  # a code update must not meet an older schema
    return {"add": _add, "remove": _remove, "list": _list}[args.cmd](args, slug)


if __name__ == "__main__":
    sys.exit(main())
