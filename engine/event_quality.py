"""What a useful event has — the one place for it: the rules a submission
must meet, the hold when it doesn't, and how each seed's events measure up.

SLT's moderation queue filled with events that said nothing: Tahoe Blue
Event Center hockey games with no description and no category, Visit Lake
Tahoe rows with no category. Their seeds had moved to deterministic adapters
(engine/seed_adapters), which skip the listing LLM that used to classify
events, and nothing flagged it. Two things here:

- **The rules** (`missing`, pure): an event is *thin* when it has no
  description worth the name (the listing's, else its detail page's —
  `description`) or no category from the town's vocabulary. Price, end time,
  accessibility and tickets are not rules: many sources honestly state none,
  and a guess is worse than a blank.
- **The hold** (`gate`, before ship — after event-read-detail had its chance
  to fill the blanks): autonomous mode only, where the moderation queue would
  otherwise be the first human to see it. A thin event not yet live is held
  for review (engine/review_hold, kind `COMPLETENESS`) instead of submitted:
  pending at `metadata-extracted`, `extra_json.completeness`, the nightly
  digest's "Held — thin event". An operator approval after the hold ships
  it as it is; a re-read that fills it releases the hold (`record`).
  `autonomy.hold_thin_events: false` turns the hold off.
- **The measure** (`by_seed`, `thin_seeds`): per seed, the share of its
  recent events that reached the detail read carrying each field — the
  daily report's "Event data by seed" and a standing alert per thin seed,
  so a seed whose adapter drops a field is named the day it starts.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

from engine import autonomy, categories as cats, event_state, review_hold
from engine.derive_helpers import dict_rows, load_event, seed_name_of
from engine.event_extract import has_clock_time
from engine.event_tickets import without_ticket_line


# --------------------------------------------------------------------------
# Thresholds — stated once.
# --------------------------------------------------------------------------

# Fewer characters than this is a name, not a description ("Hockey game.").
MIN_DESCRIPTION_CHARS = 40
# A seed is measured over the events it found in this window that reached
# the detail read, once it has at least this many…
SEED_WINDOW = timedelta(days=30)
SEED_MIN_EVENTS = 5
# …and is thin when fewer than this share of them carry a rule's field.
SEED_THIN_RATE = 0.5

# The rules, by the code a held event records.
NO_DESCRIPTION = "no-description"
NO_CATEGORY = "no-category"
REASONS = {NO_DESCRIPTION: "no description from the listing or the event's own page",
           NO_CATEGORY: "no category from the town's vocabulary"}

KIND = review_hold.COMPLETENESS
HELD_FOR_REVIEW = review_hold.HELD_FOR_REVIEW
ACCEPTED = review_hold.ACCEPTED


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------

def description(event: dict) -> Optional[str]:
    """The description event-ship sends: the listing's, else — when that is
    blank or too short to describe anything — what the event's own page says
    (event-read-detail's `detail_description`). A ticket line event-ship put
    on top doesn't count."""
    listing, page = ((without_ticket_line(event.get(k)) or "").strip()
                     for k in ("description", "detail_description"))
    if len(listing) >= MIN_DESCRIPTION_CHARS or not page:
        return listing or None
    return page


def categories(event: dict) -> list[str]:
    """The event's categories mapped onto the town's vocabulary — what
    event-ship would send."""
    return cats.normalize_category_slugs(event.get("category_slugs") or event.get("category_slug"),
                                         cats.town_category_slugs())


def missing(event: dict) -> list[str]:
    """The rules `event` fails, in rule order; [] when it is not thin. With
    no categories snapshot there is no vocabulary to judge by, so the
    category rule is skipped."""
    out = []
    if len(description(event) or "") < MIN_DESCRIPTION_CHARS:
        out.append(NO_DESCRIPTION)
    if cats.town_category_slugs() and not categories(event):
        out.append(NO_CATEGORY)
    return out


# --------------------------------------------------------------------------
# The hold (the lifecycle is engine/review_hold's)
# --------------------------------------------------------------------------

def _accepted(event: dict, mark: dict, rules: list[str]) -> bool:
    """The operator approved the event after it was held for (at least)
    these rules."""
    return set(rules) <= set(mark.get("rules") or []) and (
        mark.get("status") == ACCEPTED or review_hold.accepted(event, mark))


def hold(conn: sqlite3.Connection, event: dict, rules: list[str]) -> bool:
    """Hold `event` for the rules it fails (`review_hold.hold`). False when
    another worker or the operator moved it first. Caller commits."""
    reasons = [REASONS[r] for r in rules]
    seed = seed_name_of(event.get("source")) or None
    return review_hold.hold(conn, event, KIND, "; ".join(reasons),
                            {"rules": rules, "reasons": reasons, "seed": seed},
                            {"rules": rules, "reasons": reasons, "seed": seed})


def record(conn: sqlite3.Connection, uid: str) -> None:
    """After an extraction's upsert: a thin-event hold its row no longer
    earns (a re-read filled it) is released. Never holds — only the ship
    gate does, once the detail read has had its chance. Caller commits."""
    ev = load_event(conn, uid)
    if ev is None or ev["state"] in event_state.TERMINAL_STATES:
        return
    mark = review_hold.mark_of(ev.get("extra_json"), KIND)
    if mark.get("status") == HELD_FOR_REVIEW and not missing(ev):
        review_hold.release(conn, uid, KIND)


class Gate(NamedTuple):
    reasons: list[str]   # why the event is held now; [] — go on
    held: bool           # the hold moved it; False with reasons: it moved on first

    def __bool__(self) -> bool:
        return bool(self.reasons)


def gate(conn: sqlite3.Connection, event: dict, *, live: bool = False) -> Gate:
    """Before ship: in autonomous mode, hold a thin event that isn't live
    yet (falsy to go on). `live` — the event is already on TownLoops (a
    reconcile change, a re-ship): never held here. An operator approval
    after the hold accepts it as it is. With reasons but not `held`, another
    worker or the operator moved the event first: the caller must not let
    the drain advance it (`workqueue.RC_DEFERRED`). Caller commits."""
    s = autonomy.settings()
    if live or event.get("townloops_slug") or not (s.autonomous and s.hold_thin_events):
        return Gate([], False)
    rules = missing(event)
    if not rules:
        return Gate([], False)
    mark = review_hold.mark_of(event.get("extra_json"), KIND)
    if _accepted(event, mark, rules):
        if mark.get("status") != ACCEPTED:
            review_hold.accept(conn, event, KIND)
        return Gate([], False)
    return Gate([REASONS[r] for r in rules], hold(conn, event, rules))


def held_for_review(conn: sqlite3.Connection, upcoming: bool = True) -> list[dict]:
    """Events held as thin, still awaiting the operator — `upcoming` only
    those that can still happen (the nightly digest)."""
    return [{"event_uid": h["event_uid"], "title": h["title"], "date": h["date"],
             "seed": h["marks"][KIND.key].get("seed"),
             "rules": h["marks"][KIND.key].get("rules") or [],
             "reasons": h["marks"][KIND.key].get("reasons") or []}
            for h in review_hold.held(conn, KIND, upcoming=upcoming)]


# --------------------------------------------------------------------------
# The measure
# --------------------------------------------------------------------------

# What the per-seed table reports, in column order: label → has it.
FIELDS = {
    "description": lambda e: len(description(e) or "") >= MIN_DESCRIPTION_CHARS,
    "category":    lambda e: bool(categories(e)),
    "start time":  lambda e: has_clock_time(e.get("date_utc") or e.get("date")),
    "end time":    lambda e: bool(e.get("end_at_utc") or e.get("end_at")),
    "price":       lambda e: bool(e.get("price")),
    "tickets":     lambda e: bool(e.get("rsvp_url")),
    "access":      lambda e: bool(e.get("accessibility_notes")),
}
# The fields a rule asks for — a seed is thin on these.
RULE_FIELDS = ("description", "category")


def by_seed(conn: sqlite3.Connection, now: Optional[datetime] = None) -> list[dict]:
    """Per seed, its events first seen in SEED_WINDOW that reached the
    detail read (what event-ship would send), not declined or a duplicate:
    `seed`, `events`, `rates` {field: share 0..1}, `thin` (the RULE_FIELDS
    under SEED_THIN_RATE, once it has SEED_MIN_EVENTS). Most events first."""
    now = now or datetime.now(timezone.utc)
    since = (now - SEED_WINDOW).isoformat(timespec="seconds")
    rows = dict_rows(conn,
        "SELECT * FROM events WHERE first_seen_at >= ? AND detail_read_status IS NOT NULL "
        "AND state != 'declined' AND COALESCE(status, '') NOT IN ('duplicate', 'superseded')",
        (since,))
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(seed_name_of(r.get("source")) or r.get("source") or "?", []).append(r)
    out = []
    for seed, events in by.items():
        rates = {f: sum(1 for e in events if has(e)) / len(events) for f, has in FIELDS.items()}
        thin = [f for f in RULE_FIELDS if rates[f] < SEED_THIN_RATE
                and (f != "category" or cats.town_category_slugs())] \
            if len(events) >= SEED_MIN_EVENTS else []
        out.append({"seed": seed, "events": len(events), "rates": rates, "thin": thin})
    return sorted(out, key=lambda s: (-s["events"], s["seed"]))


def thin_seeds(conn: sqlite3.Connection, now: Optional[datetime] = None) -> list[dict]:
    return [s for s in by_seed(conn, now) if s["thin"]]


def thin_text(s: dict) -> str:
    """One line naming a thin seed and what its events lack."""
    lacks = ", ".join(f"{f} {s['rates'][f]:.0%}" for f in s["thin"])
    return (f"seed {s['seed']}: its events are thin — {lacks} of {s['events']} in "
            f"{SEED_WINDOW.days} days. Fix its adapter or listing read "
            "(engine/seed_adapters); thin events are held, not submitted")


def table(seeds: list[dict]) -> list[str]:
    """The per-seed markdown table (percentages), thin cells marked."""
    head = "| seed | events | " + " | ".join(FIELDS) + " |"
    lines = [head, "|" + "---|" * (len(FIELDS) + 2)]
    for s in seeds:
        cells = [f"**{s['rates'][f]:.0%}**" if f in s["thin"] else f"{s['rates'][f]:.0%}"
                 for f in FIELDS]
        lines.append(f"| {s['seed']} | {s['events']} | " + " | ".join(cells) + " |")
    return lines
