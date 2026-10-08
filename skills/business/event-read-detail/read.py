#!/usr/bin/env python3
"""event-read-detail — read one approved event's own detail page and store
what it states (accessibility notes). Logic lives in engine/event_detail.py."""

from __future__ import annotations

import sys

from engine import event_detail, workqueue
from engine.derive_helpers import load_event, open_db, parse_event_uid_arg, record_activity


_WORKER = "event-read-detail"
_ACTION = "read-detail"


def main() -> int:
    event_uid = parse_event_uid_arg()
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


if __name__ == "__main__":
    sys.exit(main())
