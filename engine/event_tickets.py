"""How to get in: an event's stated price and ticket/RSVP link, as shipped.

TownLoops has no ticket fields (by decision — none will be added), so the
detail-page pass (engine/event_detail.py) stores the page's own price
wording in `events.price` and the ticket/RSVP link in `events.rsvp_url`,
and event-ship carries them in what it can send:

- a free event (stated free, or $0) gets the `free` tag; an event with a
  stated price, a sold-out notice or a suggested donation never does;
- a paid, donation or RSVP event gets one line at the top of the
  description — `Tickets: $25–$40 — https://…` or `RSVP: https://…` — kept
  when the description is cut to the API limit and replaced, never stacked,
  on a re-ship or PATCH.

Both are built from the stored values at ship time, so the local
description and its content hash never change.
"""

from __future__ import annotations

import re
from typing import Optional

from engine import categories as cats


# EventPayload / EventPatch description limit.
DESCRIPTION_MAX = 5000

_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
# Things a price can be for that are not getting in.
_EXTRAS = (r"parking|wi-?fi|shuttle|snacks?|food|drinks?|beer|wine|refreshments|coffee"
           r"|concessions?|merch\w*|gifts?|t-shirts?|swag|giveaways?|samples?|raffle")
_EXTRA = re.compile(rf"\b(?:{_EXTRAS})\b", re.IGNORECASE)
_CLAUSE = re.compile(r"[;,](?!\d)|\.(?!\d)|\s[-–—]\s")
_DONATION = re.compile(r"donation|pay[- ]what[- ]you|suggested\s+(?:donation|contribution)"
                       r"|\$\s?[\d.,]+\s+suggested", re.IGNORECASE)
_SOLD_OUT = re.compile(r"(?<!not )(?<!n't )sold[- ]?out", re.IGNORECASE)
_WAITLIST = re.compile(r"wait[- ]?list|waiting list", re.IGNORECASE)
_FREE = re.compile(rf"(?<![\w-])free(?![\w-])(?!\s+(?:{_EXTRAS})\b)|no (?:charge|cost)",
                   re.IGNORECASE)
# "Free with museum admission" is not a free event.
_WITH_ADMISSION = re.compile(r"free with (?:\w+\s+){0,2}(?:admission|entry|ticket|purchase)",
                             re.IGNORECASE)
# A line event-ship put at the top of a description — exactly what
# ticket_line() writes, so a source's own "Tickets: $20 at the door." stays.
_M = r"\$\d+(?:\.\d\d)?"
_LABEL = (rf"(?:{_M}(?:–{_M})?(?: \((?:sold out|waitlist|sold out; waitlist)\))?"
          rf"|sold out(?:; waitlist)?|waitlist|by donation(?:, {_M}(?:–{_M})? suggested)?)")
_LINE = re.compile(rf"\A(?:Tickets: {_LABEL}(?: — https?://\S+)?|RSVP: https?://\S+)[ \t]*(?:\n+|\Z)")


def _amounts(price: str) -> list[float]:
    """Dollar amounts stated for getting in — not those in a clause about
    parking, drinks or the like ("Free admission; parking $5")."""
    return sorted({float(f"{whole.replace(',', '')}.{cents or '0'}")
                   for clause in _CLAUSE.split(price) if not _EXTRA.search(clause)
                   for whole, cents in _AMOUNT.findall(clause)})


def price_kind(price: Optional[str]) -> Optional[str]:
    """'free' | 'paid' | 'donation' — or None when `price` states no price.
    Any non-zero amount to get in makes it paid ("Free for kids, $15
    adults"); "donation suggested" is never free; a sold out or waitlist
    notice with no price and no "free" is paid."""
    if not price:
        return None
    if _DONATION.search(price):
        return "donation"
    amounts = _amounts(price)
    if any(amounts):
        return "paid"
    if (_FREE.search(price) and not _WITH_ADMISSION.search(price)) or amounts:
        return "free"
    if _SOLD_OUT.search(price) or _WAITLIST.search(price):
        return "paid"
    return None


def is_free(price: Optional[str]) -> bool:
    return price_kind(price) == "free"


def _money(amount: float) -> str:
    return f"${amount:.0f}" if amount == int(amount) else f"${amount:.2f}"


def _range(price: str) -> str:
    amounts = _amounts(price)
    if not any(amounts):
        return ""
    lo, hi = _money(amounts[0]), _money(amounts[-1])
    return lo if lo == hi else f"{lo}–{hi}"


def _price_label(price: str, kind: str) -> str:
    rng = _range(price)
    if kind == "donation":
        return "by donation" + (f", {rng} suggested" if rng else "")
    status = "; ".join(s for s, rx in (("sold out", _SOLD_OUT), ("waitlist", _WAITLIST))
                       if rx.search(price))
    if rng and status:
        return f"{rng} ({status})"
    return rng or status


def ticket_line(price: Optional[str], url: Optional[str]) -> Optional[str]:
    """The description's ticket line, or None when there is nothing to say.
    A stated cost is `Tickets: …`; a link with no stated cost (or a free
    event's registration link) is `RSVP: …`."""
    kind = price_kind(price)
    if kind in ("paid", "donation"):
        return f"Tickets: {_price_label(price, kind)}" + (f" — {url}" if url else "")
    if url:
        return f"RSVP: {url}"
    return None


def with_ticket_line(description: Optional[str], line: Optional[str]) -> Optional[str]:
    """`description` with `line` at the top, replacing a ticket line already
    there, the body cut (with an ellipsis) so the whole fits the API limit.
    No line → the description as it is; one is never removed."""
    if not line:
        return description
    body = _LINE.sub("", description or "", count=1).strip()
    if not body:
        return line
    room = DESCRIPTION_MAX - len(line) - 2
    if len(body) > room:
        body = body[:room - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return f"{line}\n\n{body}"


def live_tags(tags: list[str], price: Optional[str]) -> list[str]:
    """A live row's tag set brought in line with the stated price: `free`
    added (room permitting) for a free event, dropped for a priced one."""
    kind = price_kind(price)
    if kind == "free" and "free" not in tags and len(tags) < cats.MAX_TAGS:
        return [*tags, "free"]
    return [t for t in tags if t != "free"] if kind and kind != "free" else list(tags)


def ticket_tags(tags: list[str], price: Optional[str]) -> list[str]:
    """Tags as slugs with `free` first for a free event and dropped for one
    with a stated price; unchanged when no price is stated."""
    kind = price_kind(price)
    if kind == "free":
        return cats.normalize_tags(["free", *tags])
    out = cats.normalize_tags(tags)
    return [t for t in out if t != "free"] if kind else out
