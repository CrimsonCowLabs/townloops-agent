#!/usr/bin/env python3
"""autonomy — the operator's window on an autonomous town (read-only, except
`resume`, `accept` and `decline`). Logic: engine/autonomy.py.

Subcommands:
  status                      mode, knobs, circuit, pending submissions, last 24h
  why <id> [--limit N]        the recorded decisions about an event (uid prefix or
                              title words), a seed (name or seed id), a candidate
                              (uid prefix) or a TownLoops slug, newest first
  resume                      close the circuit after fixing its cause, so
                              submissions start again (the operator's call)
  accept <uid> [--note T]     approve an event held for a source mismatch
                              (engine/event_provenance) as it is
  decline <uid> [--note T]    decline one — both only for a held event (uid or
                              a unique prefix); the nightly digest lists them
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from engine import autonomy, event_provenance, event_state, moderation
from engine.derive_helpers import dict_rows, open_db, seed_ref


def _status(args) -> int:
    conn = open_db()
    s = autonomy.settings()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    print(json.dumps({
        "mode": s.mode, "settings": asdict(s), "circuit": autonomy.circuit(conn),
        "gate": asdict(autonomy.submission_gate(conn, s)),
        "last_24h": moderation.outcomes_since(conn, since),
    }, indent=2, default=str))
    return 0


def _targets(conn, query: str) -> list[str]:
    """Activity-log targets the operator's words can mean."""
    q = query.strip()
    out = [q]
    if q.isdigit():
        out += [seed_ref(r['name']) for r in dict_rows(
            conn, "SELECT name FROM event_seeds WHERE seed_id = ?", (int(q),))]
    out += [seed_ref(r['name']) for r in dict_rows(
        conn, "SELECT name FROM event_seeds WHERE lower(name) = lower(?)", (q,))]
    out += [r["local_uid"] for r in dict_rows(
        conn, "SELECT local_uid FROM submissions WHERE slug = ? AND local_uid IS NOT NULL", (q,))]
    out += [r["event_uid"] for r in dict_rows(
        conn, "SELECT event_uid FROM events WHERE COALESCE(title, name) LIKE ? "
              "ORDER BY date_utc DESC LIMIT 10", (f"%{q}%",))] if len(q) >= 4 else []
    return list(dict.fromkeys(out))


def _why(args) -> int:
    conn = open_db()
    out = []
    for t in _targets(conn, " ".join(args.id)):
        for d in autonomy.decisions(conn, target=t, limit=args.limit):
            if d not in out:
                out.append(d)
    if not out:
        print(json.dumps({"decisions": [], "note": "no recorded decision matches"}))
        return 0
    titles = {r["event_uid"]: r["title"] for r in dict_rows(
        conn, "SELECT event_uid, COALESCE(title, name) AS title FROM events WHERE event_uid IN "
              f"({', '.join('?' * len(out))})", tuple(d["target"] for d in out))}
    for d in out:
        if d["target"] in titles:
            d["title"] = titles[d["target"]]
    print(json.dumps({"decisions": sorted(out, key=lambda d: d["ts"], reverse=True)[:args.limit]},
                     indent=2, ensure_ascii=False, default=str))
    return 0


def _resume(args) -> int:
    conn = open_db()
    was = autonomy.circuit(conn)
    autonomy.close_circuit(conn, by="operator")
    print(json.dumps({"resumed": bool(was), "was": was}, default=str))
    return 0


def _resolve(args) -> int:
    """The operator's call on an event held for review: accept or decline."""
    conn = open_db()
    uids = [h["event_uid"] for h in event_provenance.held_for_review(conn)
            if h["event_uid"].startswith(args.uid)]
    if len(uids) != 1:
        print(json.dumps({"error": f"{len(uids)} held events match {args.uid!r}",
                          "held": uids[:10]}))
        return 1
    if args.cmd == "accept":
        done = event_state.approve(conn, uids[0], args.note or "[operator] accepted as is")
    else:
        done = event_state.decline(conn, uids[0], args.note or "[operator] source mismatch")
    conn.commit()
    print(json.dumps({args.cmd: uids[0], "done": done}))
    return 0 if done else 1


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="autonomy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    why = sub.add_parser("why")
    why.add_argument("id", nargs="+")
    why.add_argument("--limit", type=int, default=20)
    sub.add_parser("resume")
    for verb in ("accept", "decline"):
        p = sub.add_parser(verb)
        p.add_argument("uid")
        p.add_argument("--note")
    args = ap.parse_args(argv)
    return {"status": _status, "why": _why, "resume": _resume,
            "accept": _resolve, "decline": _resolve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
