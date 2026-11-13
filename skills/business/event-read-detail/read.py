#!/usr/bin/env python3
"""event-read-detail — read one approved event's own detail page and store
what it states (accessibility notes, price, tickets, age, and the
description/categories/tags the listing left blank). `backfill` asks the
blanks of events already read. Logic lives in engine/event_detail.py."""

from __future__ import annotations

import argparse
import sys

from engine import event_detail, workqueue
from engine.derive_helpers import load_event, open_db, parse_event_uid_arg, record_activity


_WORKER = "event-read-detail"
_ACTION = "read-detail"


def read_one(event_uid: str) -> int:
    conn = open_db()
    event = load_event(conn, event_uid)
    if not event:
        print(f"event not found: {event_uid}", file=sys.stderr)
        return 2
    result = event_detail.read_detail(conn, event, final_attempt=workqueue.is_last_attempt(conn, event_uid, _ACTION))
    if result.status == "retry":
        print(f"event {event_uid[:12]}: detail read failed, will retry: {result.error[:300]}",
              file=sys.stderr)
        return 1
    if result.status == "deferred":
        # Its hold lost a race (another worker or the operator moved it
        # first): release the claim so the drain doesn't advance it unread.
        print(f"event {event_uid[:12]}: {result.error[:300]} — not now", file=sys.stderr)
        return workqueue.RC_DEFERRED
    found = sorted(k for k, v in result.fields.items() if v)
    record_activity(
        conn, _WORKER, f"detail-{result.status}", event_uid,
        detail=f"{event.get('source_url') or '(no source_url)'}"
               + (f" → {', '.join(found)}" if found else ""),
        ok=result.status != "failed", error=result.error[:1000] or None,
    )
    print(f"event {event_uid[:12]}: detail {result.status}"
          + (f" ({', '.join(found)})" if found else ""))
    return 0


def backfill(limit: int, dry_run: bool) -> int:
    """Fill the description/categories of events read before the detail
    read asked for them — from the cached page, no fetch; one Codex call
    each. Each event is tried once."""
    conn = open_db()
    events = event_detail.backfill_candidates(conn, limit)
    filled = 0
    for ev in events:
        title = ev.get("title") or ev.get("name")
        if dry_run:
            print(f"event {ev['event_uid'][:12]}: would backfill — {title}")
            continue
        found = sorted(event_detail.backfill(conn, ev))
        filled += bool(found)
        record_activity(conn, _WORKER, event_detail.BACKFILL_ACTION, ev["event_uid"],
                        detail=f"{title} → {', '.join(found) or 'nothing'}")
        print(f"event {ev['event_uid'][:12]}: {', '.join(found) or 'nothing found'} — {title}")
    print(f"backfill: {len(events)} event(s){'' if dry_run else f', {filled} filled'}")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="read.py", description=__doc__)
    p.add_argument("--event-uid", help="read this event's page (the read-detail action)")
    sub = p.add_subparsers(dest="verb")
    b = sub.add_parser("backfill", help="fill the blanks of events already read, from the cached page",
                       description=backfill.__doc__)
    b.add_argument("--limit", type=int, default=20)
    b.add_argument("--dry-run", action="store_true")
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["backfill"] or {"-h", "--help"} & set(argv):
        args = _parser().parse_args(argv)
        return backfill(args.limit, args.dry_run)
    # The drain's call: `--event-uid <uid>` only.
    return read_one(parse_event_uid_arg())


if __name__ == "__main__":
    sys.exit(main())
