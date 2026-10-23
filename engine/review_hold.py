"""An event held for the operator's review — one lifecycle, several reasons.

A `Kind` is one reason an event can be held: a source mismatch (#45,
engine/event_provenance) or an implausible time (#46,
engine/time_plausibility). Each kind keeps its own rules and evidence in its
own module; this is what they share:

- the mark: `extra_json[<kind.key>]` with `status` `held-for-review` (pending,
  for the operator only, in both modes) or `accepted` (the operator approved
  it after the hold), plus the kind's own fields and `held_at`;
- the approval note: one `<kind.prefix>…` part per kind, joined by ` | `
  after whatever else the note says (a reconcile change) — a hold or release
  of one kind never touches another's part (`with_note` / `without_note`);
- the hold (`hold`): pending at `metadata-extracted`, its approval cleared
  (`event_state.reopen_for_review`), autonomy's verdict recorded — marked
  only if it moved the event (optimistic locking: an event another worker or
  the operator moved first is left as it is, and the caller must not let the
  drain advance it — exit `workqueue.RC_DEFERRED`);
- the acceptance (`accepted` / `accept`): only an approval made after the
  hold counts;
- the queries: `NOT_HELD_SQL` (rows the autonomous judge may decide — not
  held for any kind) and `held` (pending holds, upcoming by default: the
  nightly digest, `autonomy accept|decline`, bulk approval).

A third kind is one more `Kind` in `KINDS`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

from engine import autonomy, event_state
from engine.derive_helpers import iso_utc_now, parse_extra_json


HELD_FOR_REVIEW = "held-for-review"   # pending, for the operator only
ACCEPTED = "accepted"                 # the operator approved it after the hold

NOTE_JOIN = " | "
NOTE_MAX = 1000


@dataclass(frozen=True)
class Kind:
    key: str                                         # its extra_json key
    prefix: str                                      # its part of approval_note
    label: str                                       # what the operator is told
    judge: Callable[[dict, autonomy.Settings], autonomy.Decision]


PROVENANCE = Kind("provenance", "provenance: ", "source mismatch", autonomy.judge_provenance)
TIMES = Kind("time_check", "times: ", "implausible time", autonomy.judge_times)
KINDS = (PROVENANCE, TIMES)


def not_held_sql(kind: Kind) -> str:
    """SQL: the row is not held for `kind`."""
    return ("COALESCE(CASE WHEN json_valid(extra_json) THEN "
            f"json_extract(extra_json, '$.{kind.key}.status') END, '') != '{HELD_FOR_REVIEW}'")


# Pending rows the autonomous judge may decide: held for no kind.
NOT_HELD_SQL = "(" + " AND ".join(not_held_sql(k) for k in KINDS) + ")"


# --------------------------------------------------------------------------
# The mark
# --------------------------------------------------------------------------

def mark_of(extra_json, kind: Kind) -> dict:
    m = parse_extra_json(extra_json).get(kind.key)
    return m if isinstance(m, dict) else {}


def write(conn: sqlite3.Connection, uid: str, kind: Kind, mark: Optional[dict]) -> None:
    """Set (None: drop) the event's `kind` mark. Caller commits."""
    raw = conn.execute("SELECT extra_json FROM events WHERE event_uid = ?", (uid,)).fetchone()
    extra = parse_extra_json(raw[0] if raw else None)
    if mark is None:
        extra.pop(kind.key, None)
    else:
        extra[kind.key] = mark
    conn.execute("UPDATE events SET extra_json = ? WHERE event_uid = ?",
                 (json.dumps(extra, ensure_ascii=False), uid))


# --------------------------------------------------------------------------
# The approval note
# --------------------------------------------------------------------------

# A part starts at the note's start or after NOTE_JOIN, with a kind's prefix
# — so a part quoting " | " (a page title) stays whole.
_PART = re.compile(re.escape(NOTE_JOIN) + "(?=" + "|".join(re.escape(k.prefix) for k in KINDS)
                   + ")")


def _parts(note: Optional[str]) -> list[str]:
    return [p for p in _PART.split(note or "") if p.strip()]


def without_note(note: Optional[str], kind: Kind) -> Optional[str]:
    """`note` without `kind`'s part (None when nothing is left)."""
    return NOTE_JOIN.join(p for p in _parts(note) if not p.startswith(kind.prefix)) or None


def with_note(note: Optional[str], kind: Kind, text: str) -> str:
    """`note` with `kind`'s part set to `text`, after everything else it says."""
    base = without_note(note, kind)
    mine = kind.prefix + text
    return (f"{base}{NOTE_JOIN}{mine}" if base else mine)[:NOTE_MAX]


def with_base(note: Optional[str], base: str) -> str:
    """`note` saying `base` besides its holds' parts, which are kept (a
    reconcile re-stage rewrites what changed, never why it is held)."""
    held = [p for p in _parts(note) if any(p.startswith(k.prefix) for k in KINDS)]
    return NOTE_JOIN.join([base, *held])[:NOTE_MAX]


def _note(conn: sqlite3.Connection, uid: str) -> Optional[str]:
    r = conn.execute("SELECT approval_note FROM events WHERE event_uid = ?", (uid,)).fetchone()
    return r[0] if r else None


# --------------------------------------------------------------------------
# The lifecycle
# --------------------------------------------------------------------------

def accepted(event: dict, mark: dict) -> bool:
    """The operator approved the event after it was held for this mark: the
    hold cleared its approval, so an approval now is a new one."""
    return (mark.get("status") == HELD_FOR_REVIEW and event.get("approval_status") == "approved"
            and bool(event.get("approval_at"))
            and event["approval_at"] >= (mark.get("held_at") or ""))


def accept(conn: sqlite3.Connection, event: dict, kind: Kind) -> bool:
    """If the operator accepted the event's `kind` hold, mark it `accepted`
    (True). Caller commits."""
    mark = mark_of(event.get("extra_json"), kind)
    if not accepted(event, mark):
        return False
    write(conn, event["event_uid"], kind, {**mark, "status": ACCEPTED,
                                           "accepted_at": iso_utc_now()})
    return True


def hold(conn: sqlite3.Connection, event: dict, kind: Kind, text: str, mark: dict,
         evidence: dict) -> bool:
    """Hold `event` for `kind`: pending for the operator with `text` as its
    note part, its approval cleared, `mark` (+ status, held_at) on the row,
    autonomy's verdict on `evidence` recorded. False — nothing written — when
    another worker or the operator moved the event first. Caller commits."""
    uid = event["event_uid"]
    d = kind.judge(evidence, autonomy.settings())
    note = with_note(_note(conn, uid), kind, text)
    moved = conn.execute(
        "UPDATE events SET approval_note = ?, approval_at = NULL WHERE event_uid = ? "
        "AND state = 'metadata-extracted' AND approval_status = 'pending'",
        (note, uid)).rowcount > 0 or event_state.reopen_for_review(
            conn, uid, event_state.IN_FLIGHT_STATES, note, include_stuck=True)
    if not moved:
        return False
    write(conn, uid, kind, {**mark, "status": HELD_FOR_REVIEW, "held_at": iso_utc_now()})
    if d.verdict != autonomy.AWAIT:
        autonomy.record(conn, "event", uid, d)
    return True


def release(conn: sqlite3.Connection, uid: str, kind: Kind) -> None:
    """No `kind` hold any more: its mark and note part go, other kinds'
    stay. Caller commits."""
    write(conn, uid, kind, None)
    conn.execute("UPDATE events SET approval_note = ? WHERE event_uid = ? "
                 "AND approval_status = 'pending'", (without_note(_note(conn, uid), kind), uid))


def held(conn: sqlite3.Connection, kind: Optional[Kind] = None,
         upcoming: bool = True) -> list[dict]:
    """Events held for review (for `kind`, else for any kind), still pending
    for the operator — `upcoming` only those that can still happen (the
    nightly digest). Each: event_uid, title, date, `kinds` (held for, in
    KINDS order), `marks` ({kind.key: mark})."""
    kinds = (kind,) if kind else KINDS
    rows = conn.execute(
        "SELECT event_uid, COALESCE(title, name), COALESCE(date_local, date), extra_json "
        "FROM events WHERE state = 'metadata-extracted' AND approval_status = 'pending' "
        "AND NOT (" + " AND ".join(not_held_sql(k) for k in kinds) + ")"
        + (" AND (date_utc IS NULL OR date_utc >= ?)" if upcoming else "")
        + " ORDER BY date_utc, event_uid", (iso_utc_now(),) if upcoming else ()).fetchall()
    out = []
    for r in rows:
        marks = {k.key: mark_of(r[3], k) for k in kinds}
        out.append({"event_uid": r[0], "title": r[1], "date": r[2],
                    "kinds": [k for k in kinds if marks[k.key].get("status") == HELD_FOR_REVIEW],
                    "marks": marks})
    return out
