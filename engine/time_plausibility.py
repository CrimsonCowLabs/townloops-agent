"""An event whose times are implausible on their face is held for review (#46).

SLT shipped an AleWorX DJ set at 05:00 → 21:00: the source published
"5:00 am to 9:00 pm", almost certainly meaning 5 PM. Extraction was
faithful and the calendar entry still wrong. Whatever a source says, a
time no event plausibly keeps waits for the operator. This is not
provenance (#45: engine/event_provenance) — it catches times that are
wrong wherever they came from.

The rules (`check`, pure) and their thresholds live here and only here.
A start with no time of day — date-only, or town-local midnight, the
date-only convention (#44, engine/event_extract.day_and_clock) — has no
times to doubt.

Where it runs: after every upsert of an extraction (`record` — event-find,
event-reconcile, so a reconcile-staged change to a live row is held before
it is PATCHed or submitted), and as a gate (`gate`) before read-detail and
before ship, so no implausible time reaches TownLoops whatever approved it.

Held for review (`hold`, the lifecycle engine/review_hold shares with
provenance): pending at `metadata-extracted` with a `times: …` note part
naming each rule tripped, `extra_json.time_check.status` `held-for-review`
— in both modes. The autonomous judge skips it (`review_hold.NOT_HELD_SQL`)
and the nightly digest lists it until the operator
approves it (`accepted` for those times: a re-read giving the same times
stands, different times are checked afresh) or declines it — the dashboard,
or `autonomy accept|decline <uid>`. Only an approval after the hold counts:
the hold clears the approval it had (`event_state.reopen_for_review`). Known
early events (Tahoe 100 at 05:30) are held, approved once, and ship.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import NamedTuple, Optional

from engine import event_state, review_hold
from engine.derive_helpers import load_event
from engine.event_extract import day_and_clock, to_utc


# --------------------------------------------------------------------------
# Thresholds — a judgement call, stated once. Erring toward holding is cheap:
# a held event costs the operator one approval; a wrong time on the calendar
# costs the town a wasted trip.
# --------------------------------------------------------------------------

# A town-local start in [01:00, 06:00) is pre-dawn: almost always a source's
# AM for PM. 06:00 itself ships (SLT's Big Chief 50K and Marlette 50K start
# then); 00:01–00:59 ships (after-midnight shows); 00:00 is date-only.
PRE_DAWN_FROM = (1, 0)
PRE_DAWN_UNTIL = (6, 0)
# Longer than 14 hours is more than any one-day programme runs (an all-day
# festival, 08:00–22:00). SLT and North Shore rows longer than that were
# all wrong — office hours, a sale's whole season, a meeting with a
# series' end date. A real multi-day span or an overnight ultra is held and
# approved once.
MAX_DURATION = timedelta(hours=14)

# The rules, by the code a held event records.
PRE_DAWN = "pre-dawn-start"
ENDS_BEFORE_START = "ends-before-start"
TOO_LONG = "over-max-duration"

KIND = review_hold.TIMES
NOTE_PREFIX = KIND.prefix
HELD_FOR_REVIEW = review_hold.HELD_FOR_REVIEW   # extra_json.time_check.status
ACCEPTED = review_hold.ACCEPTED
# Pending rows not held for their times (review_hold.NOT_HELD_SQL: for any reason).
NOT_HELD_SQL = review_hold.not_held_sql(KIND)


class Trip(NamedTuple):
    rule: str     # PRE_DAWN | ENDS_BEFORE_START | TOO_LONG
    reason: str   # one sentence, for the operator


def _hm(clock: tuple[int, int]) -> str:
    return f"{clock[0]:02d}:{clock[1]:02d}"


def _instant(value) -> Optional[datetime]:
    utc = to_utc(value)
    return datetime.fromisoformat(utc) if utc else None


def check(start, end) -> list[Trip]:
    """What is implausible about an event starting `start` and ending `end`
    (extracted ISO values: naive = town-local wall clock), each rule it
    trips once, in rule order. [] when nothing is — or the start states no
    time of day. An end that states no time and falls on the start's own
    day, or isn't ISO, is no end."""
    s = day_and_clock(start)
    if not s or s[1] is None:
        return []
    out = []
    if PRE_DAWN_FROM <= s[1] < PRE_DAWN_UNTIL:
        out.append(Trip(PRE_DAWN, f"starts at {_hm(s[1])} town-local, in the pre-dawn window "
                                  f"{_hm(PRE_DAWN_FROM)}–{_hm(PRE_DAWN_UNTIL)}"))
    e = day_and_clock(end)
    if not e or (e[1] is None and e[0] == s[0]):
        return out
    start_at, end_at = _instant(start), _instant(end)
    if end_at < start_at:
        out.append(Trip(ENDS_BEFORE_START, f"ends {end} before it starts {start}"))
    elif end_at - start_at > MAX_DURATION:
        hours = (end_at - start_at).total_seconds() / 3600
        out.append(Trip(TOO_LONG, f"runs {hours:.4g}h ({start} → {end}), longer than the "
                                  f"{MAX_DURATION.total_seconds() / 3600:.4g}h an event "
                                  "plausibly runs"))
    return out


# --------------------------------------------------------------------------
# The hold (the lifecycle is engine/review_hold's, shared with provenance)
# --------------------------------------------------------------------------

def _times(event: dict) -> dict:
    return {"date": event.get("date"), "end_at": event.get("end_at") or None}


def _accepted(event: dict, tc: dict) -> bool:
    """The operator accepted these times: approved after they were held."""
    return tc.get("times") == _times(event) and (
        tc.get("status") == ACCEPTED or review_hold.accepted(event, tc))


def hold(conn: sqlite3.Connection, event: dict, trips: list[Trip]) -> bool:
    """Hold an event for the rules its times trip (`review_hold.hold`).
    False when another worker or the operator moved it first. Caller
    commits."""
    reasons = [t.reason for t in trips]
    rules = [t.rule for t in trips]
    return review_hold.hold(
        conn, event, KIND, "; ".join(reasons),
        {"rules": rules, "reasons": reasons, "times": _times(event)},
        {"rules": rules, "reasons": reasons, **_times(event)})


def record(conn: sqlite3.Connection, uid: str) -> None:
    """After an extraction's upsert: hold the row if its times are
    implausible and the operator hasn't accepted them; release a hold its
    source's times no longer trip. A shipped row changes only through a
    reconcile-staged change, which reopens it first; shipped and declined
    rows are left alone. Caller commits."""
    ev = load_event(conn, uid)
    if ev is None or ev["state"] in event_state.TERMINAL_STATES:
        return
    tc = review_hold.mark_of(ev.get("extra_json"), KIND)
    trips = check(ev.get("date"), ev.get("end_at"))
    if not trips:
        if tc:
            review_hold.release(conn, uid, KIND)
        return
    if _accepted(ev, tc):
        return
    if (tc.get("status") == HELD_FOR_REVIEW and tc.get("times") == _times(ev)
            and ev["approval_status"] == "pending"):
        return   # held already, for these times
    hold(conn, ev, trips)


class Gate(NamedTuple):
    reasons: list[str]   # why the event is held now; [] — go on
    held: bool           # the hold moved it; False with reasons: it moved on first

    def __bool__(self) -> bool:
        return bool(self.reasons)


def gate(conn: sqlite3.Connection, event: dict) -> Gate:
    """Before read-detail and ship: hold the event now if its times trip a
    rule (falsy to go on). An operator approval after the hold accepts its
    times — never re-checked while they stand. With reasons but not `held`,
    another worker or the operator moved the event first: the caller must
    not let the drain advance it (`workqueue.RC_DEFERRED`). Caller commits."""
    trips = check(event.get("date"), event.get("end_at"))
    if not trips:
        return Gate([], False)
    tc = review_hold.mark_of(event.get("extra_json"), KIND)
    if _accepted(event, tc):
        if tc.get("status") != ACCEPTED:
            review_hold.accept(conn, event, KIND)
        return Gate([], False)
    return Gate([t.reason for t in trips], hold(conn, event, trips))


def held_for_review(conn: sqlite3.Connection, upcoming: bool = True) -> list[dict]:
    """Events held for an implausible time, still awaiting the operator —
    `upcoming` only those that can still happen (the nightly digest)."""
    return [{"event_uid": h["event_uid"], "title": h["title"], "date": h["date"],
             "rules": h["marks"][KIND.key].get("rules") or [],
             "reasons": h["marks"][KIND.key].get("reasons") or []}
            for h in review_hold.held(conn, KIND, upcoming=upcoming)]
