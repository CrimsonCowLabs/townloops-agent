"""An event's source_url is the page its fields came from (#45).

Generic list-page extraction can cross-wire the cards of a multi-event page:
an event ships with its own title and venue but a neighbouring card's detail
URL (and, with it, that card's date and time). No source may ship an event
assembled from two listings, so two guards:

At extraction (`listing_events` — the one entry point event-find,
event-reconcile and the seed trial read an LLM answer through): the listing
is split into cards, one per extracted title, each ending at the next
extracted title or the next heading / list item / table row of its own
kind. An event keeps only the times its own card states, and a source_url
only if it is its card's own link (the link on or right after its title).
An event the listing can't vouch for carries `LISTING_CHECK`. Its
provenance is *established* when its title is on the listing and — if its
link had to go — its card gives its date: a card with no detail link is
the event's own page, the listing. Otherwise (`unplaced`, or a refused /
dropped link and a date its card doesn't give) it is held.
`record_extraction` writes the verdict on the row after upsert.

Before ship (`check_before_detail_read` / `detail_disagreements`, at
read-detail — so adapter rows with a detail URL are covered too; an
adapter's per-item structured read is its own provenance): the event's own
detail page must name the event in a heading and give its date in the
event's section.

Held for review (`hold_for_review`): pending at `metadata-extracted` with a
`provenance: …` note naming what disagreed, `extra_json.provenance.status`
`held-for-review` — in both modes. The autonomous judge skips it
(`NOT_HELD_SQL`) and the nightly digest lists it until the operator
approves it (accepted — never re-checked) or declines it. Acceptance needs a
real re-approval: the hold only marks an event whose state it moved, and
clears the approval it had (`event_state.reopen_for_review`).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import NamedTuple, Optional
from urllib.parse import urljoin

from engine import autonomy, blocked_page, event_state
from engine.derive_helpers import iso_utc_now, parse_extra_json
from engine.discovery import canonicalize_url
from engine.event_extract import day_and_clock, parse_json_array, published_times
from engine.page_extract import PAGE_URL, normalize, plain_text


# The key `listing_events` sets on an extracted event its listing card does
# not vouch for: {"problem": refused | not-in-card | not-on-page | unplaced,
# "url", "belongs_to" (refused: the card the link is), "established",
# "reason"}.
LISTING_CHECK = "listing_check"
NOTE_PREFIX = "provenance: "

# extra_json.provenance.status
HELD_FOR_REVIEW = "held-for-review"   # pending, for the operator only
ACCEPTED = "accepted"                 # the operator approved it after the hold
URL_DROPPED = "url-dropped"           # its link went; its listing card vouches for it
LIVE_MISMATCH = "live-mismatch"       # shipped, and its source_url is another card's

# Pending rows the autonomous judge may decide: not held for the operator.
NOT_HELD_SQL = ("COALESCE(CASE WHEN json_valid(extra_json) THEN "
                "json_extract(extra_json, '$.provenance.status') END, '') != "
                f"'{HELD_FOR_REVIEW}'")


def canon_url(url, base: Optional[str] = None) -> Optional[str]:
    """`url` (relative to `base`, if given) as compared: canonical http(s),
    None for anything else."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = urljoin(base, url.strip()) if base else url.strip()
    if not url.startswith(("http://", "https://")):
        return None
    try:
        return canonicalize_url(url)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# At extraction: each event is read against its own card
# --------------------------------------------------------------------------

# A markdown link target, or a bare URL; an image, whose source is no link.
_LINK = re.compile(r"\]\(\s*<?(?P<t>[^)\s>]+)|(?P<b>" + PAGE_URL.pattern + ")")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# A line that starts a block: a heading (its level), a top-level list item,
# a table row.
_BLOCK = re.compile(r"^(?:[ \t]{0,3}(?P<h>#{1,6})[ \t]|(?P<li>[-*+][ \t]|\d+[.)][ \t])|(?P<row>\|))",
                    re.MULTILINE)
# What may sit between a title's own link and the title: closing markup.
_JOIN = re.compile(r"[\s)\]#*_\[|>-]*")
# A line that is a linked image — a card's picture, just above its title.
_LINKED_IMAGE = re.compile(r"[ \t]*\[!\[")
_STOP = {"the", "and", "with", "for", "from", "at", "of", "on", "in", "to", "an", "a", "by",
         "our", "your", "featuring", "feat", "presents", "live"}


class _Link(NamedTuple):
    start: int
    end: int
    url: str


class _Card(NamedTuple):
    start: int
    end: int
    key: str
    anchors: tuple[int, ...]   # where its title stands in it


def _words(text) -> list[str]:
    return re.findall(r"[^\W_]+", text.lower() if isinstance(text, str) else "")


def _fold(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _significant(title) -> list[str]:
    return [w for w in _words(title) if len(w) > 2 and w not in _STOP] or _words(title)


def _title_pattern(words: list[str]) -> re.Pattern:
    return re.compile(r"(?<![^\W_])" + r"[\W_]+".join(map(re.escape, words)) + r"(?![^\W_])")


def _mask(page: str, base: str) -> tuple[list[_Link], str]:
    """Every link on `page` (images' sources are not links), and the page
    with link targets and images blanked — titles are looked for in the
    text, never in a URL slug or an image's alt text."""
    images = [m.span() for m in _IMAGE.finditer(page)]
    masked = list(page)
    for s, e in images:
        masked[s:e] = " " * (e - s)
    links = []
    for m in _LINK.finditer(page):
        group = "t" if m.group("t") else "b"
        start, end = m.span(group)
        masked[start:end] = " " * (end - start)
        if any(s <= m.start() < e for s, e in images):
            continue
        url = canon_url(m.group(group).rstrip(".,;:"), base)
        if url:
            links.append(_Link(start, end, url))
    return links, "".join(masked)


def _block_kind(text: str, pos: int) -> tuple[str, int]:
    """The kind of the line `pos` is on: ("h", level) | ("li", 0) |
    ("row", 0) | ("text", 0)."""
    line = text.rfind("\n", 0, pos) + 1
    m = _BLOCK.match(text, line)
    if not m:
        return "text", 0
    if m.group("h"):
        return "h", len(m.group("h"))
    return ("li" if m.group("li") else "row"), 0


def _structural_end(text: str, pos: int, blocks: list[tuple[int, str, int]]) -> int:
    """Where the card whose title is at `pos` ends by the page's structure:
    the next block of its own kind — a heading no deeper than its own, the
    next top-level list item or table row — or any heading after a title in
    plain text."""
    kind, level = _block_kind(text, pos)
    line_end = text.find("\n", pos)
    for start, k, lv in blocks:
        if line_end < 0 or start <= line_end:
            continue
        if k == "h" and (kind != "h" or lv <= level):
            return start
        if k == kind and kind in ("li", "row"):
            return start
    return len(text)


def _place(text: str, words: list[str]) -> list[tuple[int, int]]:
    """Where a title stands on the page: its words in order, else a line
    holding all its significant words (a title the model tidied)."""
    hits = [m.span() for m in _title_pattern(words).finditer(text)]
    if hits:
        return hits
    wanted = {_fold(w) for w in _significant(" ".join(words))}
    out, pos = [], 0
    for line in text.split("\n"):
        if wanted and wanted <= {_fold(w) for w in _words(line)}:
            out.append((pos, pos + len(line)))
        pos += len(line) + 1
    return out


def _cards(text: str, keys: dict[str, list[str]]) -> list[_Card]:
    """The page split into the extracted events' cards: from where a title
    stands to the next different extracted title or the end of its block
    (`_structural_end`). Between cards is no event's."""
    lower = text.lower()
    hits = sorted(((s, e, k) for k, words in keys.items() for s, e in _place(lower, words)),
                  key=lambda h: (h[0], -h[1]))
    anchors: list[tuple[int, int, str]] = []
    for h in hits:   # a title inside a longer one ("Jazz" in "Jazz Night") is the longer's
        if anchors and h[0] < anchors[-1][1]:
            continue
        anchors.append(h)
    blocks = [(m.start(), "h" if m.group("h") else "li" if m.group("li") else "row",
               len(m.group("h") or "")) for m in _BLOCK.finditer(text)]
    cards: list[_Card] = []
    for i, (start, _end, key) in enumerate(anchors):
        nxt = next((a[0] for a in anchors[i + 1:] if a[2] != key), len(text))
        end = min(nxt, _structural_end(text, start, blocks))
        if cards and cards[-1].key == key and start < cards[-1].end:
            last = cards[-1]
            cards[-1] = _Card(last.start, max(last.end, end), key, last.anchors + (start,))
        else:
            cards.append(_Card(start, end, key, (start,)))
    return cards


class _Listing:
    """One listing page read against the events extracted from it."""

    def __init__(self, page: str, base_url: str, events: list):
        self.base = base_url
        self.listing = canon_url(base_url)
        self.page = page or ""
        self.links, self.text = _mask(self.page, base_url)
        keys, self.shown = {}, {}
        for e in events:
            words = _words(e.get("title") if isinstance(e, dict) else "")
            if words:
                keys.setdefault(" ".join(words), words)
                self.shown.setdefault(" ".join(words), e["title"])
        self.cards = _cards(self.text, keys)

    def cards_of(self, key: str) -> list[_Card]:
        return [c for c in self.cards if c.key == key]

    def _own_links(self, card: _Card) -> set[str]:
        """A card's own link(s): the first link at or after each place its
        title stands, and a link right before it (an image linking the
        title)."""
        out = set()
        for a in card.anchors:
            after = next((ln for ln in self.links if a <= ln.start < card.end), None)
            if after:
                out.add(after.url)
            before = [ln for ln in self.links if ln.end <= a]
            if before and _JOIN.fullmatch(self.text[before[-1].end:a]) and _LINKED_IMAGE.match(
                    self.page, self.page.rfind("\n", 0, before[-1].start) + 1):
                out.add(before[-1].url)
        return out

    def link_problem(self, key: str, url: str) -> Optional[tuple[str, Optional[str]]]:
        """None when `url` is the event's own card link; else (problem, the
        title of the card it is, for a refusal)."""
        if any(url in self._own_links(c) for c in self.cards_of(key)):
            return None
        if not any(ln.url == url for ln in self.links):
            return "not-on-page", None
        for c in self.cards:
            if c.key != key and url in self._own_links(c):
                return "refused", self.shown[c.key]
        positions = [ln.start for ln in self.links if ln.url == url]
        for c in self.cards:
            if c.key != key and any(c.start <= p < c.end for p in positions):
                return "refused", self.shown[c.key]
        return "not-in-card", None

    def check(self, e: dict) -> dict:
        title = e.get("title")
        key = " ".join(_words(title))
        if not key:
            return e
        cards = self.cards_of(key)
        card_text = "\n".join(self.text[c.start:c.end] for c in cards) if cards else self.text
        e = published_times(e, card_text)
        url = e.get("source_url")
        if not cards:
            return {**e, LISTING_CHECK: {
                "problem": "unplaced", "url": url, "belongs_to": None, "established": False,
                "reason": f'"{title}" is not on the listing {self.base}'}}
        if not url:
            return e
        canon = canon_url(url, self.base)
        if canon and canon == self.listing:
            return e
        problem = self.link_problem(key, canon) if canon else ("not-on-page", None)
        if problem is None:
            return e
        day = (e.get("date") or "")[:10]
        established = _gives(card_text, day)
        check = {"problem": problem[0], "url": url, "belongs_to": problem[1],
                 "established": established}
        check["reason"] = _listing_reason(check) + (
            "" if established else f"; its listing card does not give {day or 'its date'}")
        return {**e, "source_url": None, LISTING_CHECK: check}


def _listing_reason(check: dict) -> str:
    url = check.get("url")
    return {
        "refused": f'listing link {url} is in the card of "{check.get("belongs_to")}", '
                   "not this event's",
        "not-in-card": f"listing link {url} is not this event's own link on the listing",
        "not-on-page": f"listing link {url} is not on the listing",
    }.get(check.get("problem"), check.get("reason") or "")


def _gives(text: str, day: str) -> bool:
    """`text` states the ISO `day`."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        return False
    return (int(day[5:7]), int(day[8:10])) in page_dates(text)


def listing_events(stdout: str, page: str, base_url: str) -> list:
    """An extraction's JSON array read against the listing `page` it came
    from (at `base_url`): each event's times held to what its own card
    states (engine/event_extract.published_times), its source_url to its
    card's own link, and `LISTING_CHECK` on an event the listing doesn't
    vouch for."""
    events = parse_json_array(stdout)
    listing = _Listing(page, base_url, events)
    return [listing.check(e) if isinstance(e, dict) else e for e in events]


def _provenance(extra_json) -> dict:
    p = parse_extra_json(extra_json).get("provenance")
    return p if isinstance(p, dict) else {}


def _write(conn: sqlite3.Connection, uid: str, prov: Optional[dict]) -> None:
    raw = conn.execute("SELECT extra_json FROM events WHERE event_uid = ?", (uid,)).fetchone()
    extra = parse_extra_json(raw[0] if raw else None)
    if prov is None:
        extra.pop("provenance", None)
    else:
        extra["provenance"] = prov
    conn.execute("UPDATE events SET extra_json = ? WHERE event_uid = ?",
                 (json.dumps(extra, ensure_ascii=False), uid))


def _event(conn: sqlite3.Connection, uid: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,))
    r = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], r)) if r else None


def record_extraction(conn: sqlite3.Connection, uid: str, evt: dict) -> None:
    """After upsert, the listing's verdict on the row. Not vouched for: held
    for review. A dropped link its card vouches without: the link comes off
    (`url-dropped`). A refused link a shipped row carries: `live-mismatch`,
    for the operator (live is never touched here). A clean extraction clears
    an earlier listing verdict; an acceptance or a detail-page hold stands.
    Caller commits."""
    ev = _event(conn, uid)
    if ev is None:
        return
    check = evt.get(LISTING_CHECK)
    if check and check.get("problem") == "refused":
        _flag_live(conn, ev, check)
    prov = _provenance(ev["extra_json"])
    status = prov.get("status")
    if ev["state"] in ("shipped", "declined") or status == ACCEPTED:
        return
    listing_hold = (status == HELD_FOR_REVIEW and prov.get("stage") == "listing"
                    and not operator_accepted(ev, prov))
    if status == HELD_FOR_REVIEW and not listing_hold:
        return
    if check and check.get("url") and check["problem"] != "unplaced":
        conn.execute("UPDATE events SET source_url = NULL WHERE event_uid = ? AND source_url = ?",
                     (uid, check["url"]))
    if check and not check["established"]:
        if not listing_hold:
            hold_for_review(conn, ev, [check["reason"]], check.get("url"), stage="listing")
        return
    if listing_hold or status == URL_DROPPED:   # the listing vouches for it now
        _write(conn, uid, None)
        conn.execute(
            "UPDATE events SET source_url = COALESCE(source_url, ?), "
            "  approval_note = CASE WHEN approval_note LIKE ? THEN NULL ELSE approval_note END "
            "WHERE event_uid = ?",
            (evt.get("source_url") or None, NOTE_PREFIX + "%", uid))
    if check:
        _write(conn, uid, {"status": URL_DROPPED, **check, "at": iso_utc_now()})


def _flag_live(conn: sqlite3.Connection, ev: dict, check: dict) -> None:
    """Shipped rows of this seed carrying a link the listing shows is
    another card's own: `live-mismatch`, listed in the nightly digest."""
    canon = canon_url(check["url"])
    belongs = " ".join(_words(check.get("belongs_to")))
    for r in conn.execute(
            "SELECT event_uid, COALESCE(title, name), source_url, extra_json FROM events "
            "WHERE state = 'shipped' AND source = ? AND source_url IS NOT NULL",
            (ev["source"],)).fetchall():
        if canon_url(r[2]) != canon or " ".join(_words(r[1])) == belongs:
            continue
        if _provenance(r[3]).get("status") in (LIVE_MISMATCH, ACCEPTED):
            continue
        _write(conn, r[0], {"status": LIVE_MISMATCH, "url": r[2],
                            "belongs_to": check.get("belongs_to"),
                            "reason": _listing_reason({**check, "url": r[2]}),
                            "at": iso_utc_now()})


# --------------------------------------------------------------------------
# Before ship: the detail page agrees with the event
# --------------------------------------------------------------------------

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
          r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_MONTH_DAY = re.compile(
    rf"\b(?P<m>{_MONTH})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b"
    rf"(?:\s*(?:-|–|—|to|through|thru)\s*(?:(?P<m2>{_MONTH})\.?\s+)?(?P<d2>\d{{1,2}})\b(?!:))?",
    re.IGNORECASE)
_DAY_MONTH = re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m>{_MONTH})\b",
                        re.IGNORECASE)
_NUMERIC = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?:\d{4}|\d{2})\b")
_ISO = re.compile(r"\b\d{4}-(?P<m>\d{2})-(?P<d>\d{2})\b")
_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
# A heading that starts the page's other content: a list of other events.
_ASIDE = re.compile(
    r"\b(?:upcoming|related|other|more|similar|featured|recent|nearby|popular|past)\b.*"
    r"\b(?:events?|posts?|news|listings?|stories|articles)\b"
    r"|\byou (?:may|might) also\b|\bsee also\b", re.IGNORECASE)
# A line dating the page, not the event.
_STAMP = re.compile(r"\b(?:posted|updated|published|modified|created|copyright)\b|©",
                    re.IGNORECASE)


def _month(name: str) -> int:
    return _MONTHS.index(name[:3].lower()) + 1


def page_dates(page: str) -> set[tuple[int, int]]:
    """Every (month, day) the page states — "Oct 9", "October 16-18",
    "9 October", "10/09/2026", "2026-10-09"."""
    out: set[tuple[int, int]] = set()

    def add(m: int, d: int) -> None:
        if 1 <= m <= 12 and 1 <= d <= 31:
            out.add((m, d))

    for x in _MONTH_DAY.finditer(page):
        m, d = _month(x["m"]), int(x["d"])
        add(m, d)
        if x["d2"]:
            m2, d2 = (_month(x["m2"]) if x["m2"] else m), int(x["d2"])
            if m2 == m and d < d2 <= 31:
                for day in range(d + 1, d2 + 1):
                    add(m, day)
            elif m2 != m:
                for day in range(d + 1, 32):
                    add(m, day)
                for day in range(1, d2 + 1):
                    add(m2, day)
    for x in _DAY_MONTH.finditer(page):
        add(_month(x["m"]), int(x["d"]))
    for rx in (_NUMERIC, _ISO):
        for x in rx.finditer(page):
            add(int(x["m"]), int(x["d"]))
    return out


def _names(title: str, text: str) -> bool:
    """`text` names the event: its title's significant words are in it —
    all of them for a one- or two-word title, two in three otherwise."""
    words = _significant(title)
    if not words:
        return True
    there = {_fold(w) for w in _words(normalize(text))}
    hits = sum(_fold(w) in there for w in words)
    return hits == len(words) if len(words) <= 2 else hits * 3 >= len(words) * 2


class _Heading(NamedTuple):
    start: int
    level: int
    text: str


def _headings(page: str) -> list[_Heading]:
    return [_Heading(m.start(), len(m.group(1)), plain_text(m.group(2))[:120])
            for m in _HEADING.finditer(page or "")]


def event_section(title: str, page: str) -> tuple[bool, str]:
    """(the page names the event, the part of it about the event). A page
    with headings names it in one; its section runs to the next heading no
    deeper, or one that starts a list of other events. A page with no
    headings is judged whole."""
    heads = _headings(page)
    if not heads:
        return _names(title, page), page
    for i, h in enumerate(heads):
        if _names(title, h.text):
            end = next((n.start for n in heads[i + 1:]
                        if n.level <= h.level or _ASIDE.search(n.text)), len(page))
            return True, page[h.start:end]
    # A title set as a line of its own ("**Line Dancing**"), not a list item.
    most = len(_words(title)) + 2
    pos = 0
    for line in page.splitlines(keepends=True):
        text = plain_text(line)
        if (text and not _BLOCK.match(line) and len(_words(text)) <= most
                and _names(title, text)):
            end = next((n.start for n in heads if n.start > pos), len(page))
            return True, page[pos:end]
        pos += len(line)
    return False, page


def _shared_page(conn: sqlite3.Connection, event: dict) -> bool:
    """Another occurrence of the same event links the same page — a Series'
    page, which gives one date (or the rule), not each occurrence's."""
    title = event.get("title") or event.get("name") or ""
    return conn.execute(
        "SELECT 1 FROM events WHERE source_url = ? AND event_uid != ? "
        "AND lower(COALESCE(title, name)) = lower(?) LIMIT 1",
        (event.get("source_url"), event["event_uid"], title)).fetchone() is not None


def detail_disagreements(conn: sqlite3.Connection, event: dict, page: str) -> list[str]:
    """What the event's detail page contradicts, one sentence each: no
    heading names the event, or its section states dates (other than when
    the page was posted or updated) and none is the event's. [] when it
    agrees — or when the fetch got a challenge/error page, which says
    nothing either way."""
    if not page or blocked_page.reason(page, None, None):
        return []
    out = []
    title = event.get("title") or event.get("name") or ""
    named, section = event_section(title, page) if title else (True, page)
    if not named:
        heads = _headings(page)
        out.append(f'detail page does not name "{title}"'
                   + (f' (it reads "{heads[0].text}")' if heads else ""))
    day = (day_and_clock(event.get("date_local") or event.get("date")) or ("",))[0]
    if day and not _shared_page(conn, event):
        stated = page_dates("\n".join(ln for ln in section.splitlines() if not _STAMP.search(ln)))
        if stated and (int(day[5:7]), int(day[8:10])) not in stated:
            shown = ", ".join(f"{_MONTHS[m - 1].title()} {d}" for m, d in sorted(stated)[:5])
            out.append(f"detail page does not give {day} (it gives {shown})")
    return out


def operator_accepted(event: dict, prov: dict) -> bool:
    """The operator approved the event after it was held: the hold cleared
    its approval, so an approval now is a new one."""
    return (prov.get("status") == HELD_FOR_REVIEW and event.get("approval_status") == "approved"
            and bool(event.get("approval_at")) and event["approval_at"] >= (prov.get("held_at") or ""))


def check_before_detail_read(conn: sqlite3.Connection, event: dict) -> tuple[bool, list[str]]:
    """Before read-detail: (judge the page?, reasons to hold it now). An
    event the operator re-approved after a hold is accepted — never
    re-checked. One still marked held without that (approved before the
    hold) is held again."""
    prov = _provenance(event.get("extra_json"))
    status = prov.get("status")
    if status == HELD_FOR_REVIEW:
        if operator_accepted(event, prov):
            _write(conn, event["event_uid"], {**prov, "status": ACCEPTED,
                                              "accepted_at": iso_utc_now()})
            return False, []
        return False, prov.get("disagreed") or ["held for review, not re-approved since"]
    return status != ACCEPTED, []


def hold_for_review(conn: sqlite3.Connection, event: dict, disagreed: list[str],
                    url: Optional[str], stage: str = "detail") -> bool:
    """Hold an event whose provenance failed (`stage`: listing | detail),
    naming what disagreed: pending for the operator, its approval cleared,
    with autonomy's verdict recorded. Marked only if the hold moved it — an
    event another worker or the operator moved first is left as it is.
    Caller commits."""
    uid = event["event_uid"]
    note = NOTE_PREFIX + "; ".join(disagreed)
    if url and url not in note:
        note += f" — source_url {url}"
    evidence = {"disagreed": disagreed, "source_url": url, "stage": stage}
    d = autonomy.judge_provenance(evidence, autonomy.settings())
    now = iso_utc_now()
    moved = conn.execute(
        "UPDATE events SET approval_note = ?, approval_at = NULL WHERE event_uid = ? "
        "AND state = 'metadata-extracted' AND approval_status = 'pending'",
        (note[:1000], uid)).rowcount > 0 or event_state.reopen_for_review(
            conn, uid, event_state.IN_FLIGHT_STATES, note[:1000], include_stuck=True)
    if not moved:
        return False
    _write(conn, uid, {"status": HELD_FOR_REVIEW, "stage": stage, "disagreed": disagreed,
                       "source_url": url, "held_at": now})
    if d.verdict != autonomy.AWAIT:
        autonomy.record(conn, "event", uid, d)
    return True


def held_for_review(conn: sqlite3.Connection) -> list[dict]:
    """Every event held for a source mismatch, still awaiting the operator."""
    rows = conn.execute(
        "SELECT event_uid, COALESCE(title, name), COALESCE(date_local, date), extra_json "
        f"FROM events WHERE state = 'metadata-extracted' AND NOT ({NOT_HELD_SQL}) "
        "ORDER BY date_utc, event_uid").fetchall()
    out = []
    for r in rows:
        prov = _provenance(r[3])
        out.append({"event_uid": r[0], "title": r[1], "date": r[2],
                    "source_url": prov.get("source_url"),
                    "disagreed": prov.get("disagreed") or []})
    return out


def live_mismatches(conn: sqlite3.Connection) -> list[dict]:
    """Upcoming shipped events whose source_url the listing shows is another
    card's own — live, for the operator (#47 audits live)."""
    rows = conn.execute(
        "SELECT event_uid, COALESCE(title, name), COALESCE(date_local, date), townloops_slug, "
        "  extra_json FROM events WHERE state = 'shipped' "
        "  AND (date_utc IS NULL OR date_utc >= ?) "
        "  AND CASE WHEN json_valid(extra_json) THEN "
        "    json_extract(extra_json, '$.provenance.status') END = ? "
        "ORDER BY date_utc, event_uid", (iso_utc_now(), LIVE_MISMATCH)).fetchall()
    return [{"event_uid": r[0], "title": r[1], "date": r[2], "townloops_slug": r[3],
             "reason": _provenance(r[4]).get("reason")} for r in rows]
