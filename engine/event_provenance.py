"""An event's source_url is the page its fields came from (#45).

Generic list-page extraction can cross-wire the cards of a multi-event page:
an event ships with its own title and venue but a neighbouring card's detail
URL (and, with it, that card's date and time). No source may ship an event
assembled from two listings, so two guards:

At extraction (`check_card_urls`, event-find's and event-reconcile's
generic path): a source_url is kept only when it is a link inside the
event's own card on the listing — the page split into cards at each
extracted title. A link that is in another extracted event's card is
refused and named (`REFUSED`); a link not on the page at all is dropped.
`record_extraction` writes the verdict on the row (`extra_json.provenance`,
the stale URL blanked, the pending card's `approval_note`).

Before ship (`gate`/`detail_disagreements`, at read-detail — so it covers
adapters too): the event's own detail page must name the event and give its
date. A refused link, or a page that disagrees, holds the event with what
disagreed (`hold`): supervised, back to the operator's approval queue with
the reason as its note — re-approving accepts it; autonomous, declined with
a recorded `provenance-mismatch` decision. The nightly digest lists them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional
from urllib.parse import urljoin

from engine import blocked_page
from engine.derive_helpers import iso_utc_now, parse_extra_json
from engine.discovery import canonicalize_url
from engine.page_extract import normalize


# The key check_card_urls sets on an event whose link was another card's:
# {"url": the refused link, "belongs_to": that card's title}.
REFUSED = "_source_url_refused"
NOTE_PREFIX = "provenance: "

REFUSED_URL, HELD, ACCEPTED = "refused-url", "held", "accepted"


# --------------------------------------------------------------------------
# At extraction: the link is in the event's own card
# --------------------------------------------------------------------------

# A markdown link target, or a bare URL.
_LINK = re.compile(r"\]\(\s*<?(?P<t>[^)\s>]+)|(?P<b>https?://[^\s)\]>\"'<]+)")


def _canon(url, base: str) -> Optional[str]:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        return canonicalize_url(urljoin(base, url.strip()))
    except ValueError:
        return None


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.lower() if isinstance(text, str) else "")


def _title_pattern(words: list[str]) -> re.Pattern:
    return re.compile(r"(?<![^\W_])" + r"[\W_]+".join(map(re.escape, words)) + r"(?![^\W_])")


def _links(page: str, base: str) -> tuple[list[tuple[int, str]], str]:
    """(position, canonical URL) of every link on `page`, and the page
    lowercased with the link targets blanked (titles are looked for in the
    text, never in a URL slug)."""
    found, masked = [], list(page.lower())
    for m in _LINK.finditer(page):
        group = "t" if m.group("t") else "b"
        start, end = m.span(group)
        canon = _canon(m.group(group), base)
        if canon:
            found.append((start, canon))
        masked[start:end] = " " * (end - start)
    return found, "".join(masked)


def _cards(text: str, keys: dict[str, re.Pattern]) -> list[tuple[int, int, str]]:
    """The page split into cards: (start, end, title key), a card running
    from where an extracted title appears to where a different one does."""
    hits = sorted(((m.start(), m.end(), k) for k, p in keys.items() for m in p.finditer(text)),
                  key=lambda h: (h[0], -h[1]))
    anchors: list[tuple[int, int, str]] = []
    for h in hits:   # a title inside a longer one ("Jazz" in "Jazz Night") is the longer's
        if anchors and h[0] < anchors[-1][1]:
            continue
        anchors.append(h)
    cards: list[tuple[int, int, str]] = []
    for start, _end, key in anchors:
        if cards and cards[-1][2] == key:
            continue
        if cards:
            cards[-1] = (cards[-1][0], start, cards[-1][2])
        cards.append((start, len(text), key))
    return cards


def _owner(positions: list[int], cards: list[tuple[int, int, str]]) -> Optional[str]:
    """The card a link belongs to: of the cards it appears in, the one where
    it sits closest to the card's title."""
    best = None
    for pos in positions:
        for start, end, key in cards:
            if start <= pos < end and (best is None or pos - start < best[0]):
                best = (pos - start, key)
    return best[1] if best else None


def check_card_urls(events: list[dict], page: str, base_url: str) -> list[dict]:
    """`events` (extracted from listing `page`, at `base_url`) with each
    source_url kept only if it is a link in that event's own card. A link
    in another extracted event's card is blanked and named under `REFUSED`;
    one not on the page is blanked. The listing's own URL, and an event
    whose title isn't on the page (its card can't be placed), keep theirs."""
    links, text = _links(page or "", base_url)
    keys, shown = {}, {}
    for e in events:
        words = _words(e.get("title") if isinstance(e, dict) else "")
        if words:
            key = " ".join(words)
            keys.setdefault(key, _title_pattern(words))
            shown.setdefault(key, e["title"])
    cards = _cards(text, keys)
    placed = {c[2] for c in cards}
    listing = _canon(base_url, base_url)
    out = []
    for e in events:
        if not isinstance(e, dict) or not e.get("source_url"):
            out.append(e)
            continue
        canon = _canon(e["source_url"], base_url)
        if canon == listing:
            out.append(e)
            continue
        positions = [pos for pos, c in links if c == canon]
        if not positions:
            out.append({**e, "source_url": None})
            continue
        key = " ".join(_words(e.get("title")))
        owner = _owner(positions, cards)
        if key in placed and owner is not None and owner != key:
            out.append({**e, "source_url": None,
                        REFUSED: {"url": e["source_url"], "belongs_to": shown[owner]}})
            continue
        out.append(e)
    return out


def refused_reason(refused: dict) -> str:
    return (f'listing link {refused.get("url")} is in the card of '
            f'"{refused.get("belongs_to")}", not this event\'s')


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


def record_extraction(conn: sqlite3.Connection, uid: str, evt: dict) -> None:
    """After upsert: a refused link marks the row (the refused URL comes off a
    row not shipped yet; a pending row's note says why). A clean extraction
    clears an earlier refusal. An operator's acceptance, or a hold already
    made, stands. Caller commits."""
    row = conn.execute("SELECT extra_json FROM events WHERE event_uid = ?", (uid,)).fetchone()
    if row is None:
        return
    prov = _provenance(row[0])
    refused = evt.get(REFUSED)
    if prov.get("status") in (HELD, ACCEPTED):
        return
    if refused:
        _write(conn, uid, {"status": REFUSED_URL, **refused, "at": iso_utc_now()})
        conn.execute(
            "UPDATE events SET "
            "  source_url = CASE WHEN source_url = ? AND state != 'shipped' THEN NULL "
            "    ELSE source_url END, "
            "  approval_note = CASE WHEN approval_status = 'pending' THEN ? ELSE approval_note END "
            "WHERE event_uid = ?",
            (refused["url"], NOTE_PREFIX + refused_reason(refused), uid))
    elif prov.get("status") == REFUSED_URL:
        _write(conn, uid, None)
        conn.execute(
            "UPDATE events SET source_url = COALESCE(source_url, ?), "
            "  approval_note = CASE WHEN approval_note LIKE ? THEN NULL ELSE approval_note END "
            "WHERE event_uid = ?",
            (evt.get("source_url") or None, NOTE_PREFIX + "%", uid))


# --------------------------------------------------------------------------
# Before ship: the detail page agrees with the event
# --------------------------------------------------------------------------

_STOP = {"the", "and", "with", "for", "from", "at", "of", "on", "in", "to", "an", "a", "by",
         "our", "your", "featuring", "feat", "presents", "live"}
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
_HEADING = re.compile(r"^\s*#{1,3}\s+(.+?)\s*#*\s*$", re.MULTILINE)


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


def _fold(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def names_event(title: str, page: str) -> bool:
    """The page names the event: its title's significant words are on it —
    all of them for a one- or two-word title, two in three otherwise."""
    words = [w for w in _words(title) if len(w) > 2 and w not in _STOP] or _words(title)
    if not words:
        return True
    on_page = {_fold(w) for w in _words(normalize(page))}
    hits = sum(_fold(w) in on_page for w in words)
    return hits == len(words) if len(words) <= 2 else hits * 3 >= len(words) * 2


def _heading(page: str) -> Optional[str]:
    m = _HEADING.search(page or "")
    if not m:
        return None
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", m.group(1))
    return re.sub(r"[*_`]+", "", text).strip()[:120] or None


def _shared_page(conn: sqlite3.Connection, event: dict) -> bool:
    """Another occurrence of the same event links the same page — a Series'
    page, which gives one date (or the rule), not each occurrence's."""
    title = event.get("title") or event.get("name") or ""
    return conn.execute(
        "SELECT 1 FROM events WHERE source_url = ? AND event_uid != ? "
        "AND lower(COALESCE(title, name)) = lower(?) LIMIT 1",
        (event.get("source_url"), event["event_uid"], title)).fetchone() is not None


def detail_disagreements(conn: sqlite3.Connection, event: dict, page: str) -> list[str]:
    """What the event's detail page contradicts, one sentence each: it does
    not name the event, or it states dates and none is the event's. [] when
    it agrees — or when the fetch got a challenge/error page, which says
    nothing either way."""
    if not page or blocked_page.reason(page, None, None):
        return []
    out = []
    title = event.get("title") or event.get("name") or ""
    if title and not names_event(title, page):
        heading = _heading(page)
        out.append(f'detail page does not name "{title}"'
                   + (f' (it reads "{heading}")' if heading else ""))
    from engine.event_extract import day_and_clock
    day = (day_and_clock(event.get("date_local") or event.get("date")) or ("",))[0]
    if day and not _shared_page(conn, event):
        stated = page_dates(page)
        if stated and (int(day[5:7]), int(day[8:10])) not in stated:
            shown = ", ".join(f"{_MONTHS[m - 1].title()} {d}" for m, d in sorted(stated)[:5])
            out.append(f"detail page does not give {day} (it gives {shown})")
    return out


def gate(conn: sqlite3.Connection, event: dict) -> tuple[bool, Optional[str]]:
    """Before read-detail: (check the page?, a hold reason now). An event
    re-approved after a hold is the operator's acceptance — never re-checked;
    a link refused at extraction holds at once."""
    prov = _provenance(event.get("extra_json"))
    status = prov.get("status")
    if status == HELD:   # read-detail serves only approved events: re-approved
        _write(conn, event["event_uid"], {**prov, "status": ACCEPTED, "accepted_at": iso_utc_now()})
        return False, None
    if status == ACCEPTED:
        return False, None
    if status == REFUSED_URL:
        return False, refused_reason(prov)
    return True, None


def hold(conn: sqlite3.Connection, event: dict, disagreed: list[str],
         url: Optional[str]) -> str:
    """Hold an approved event whose provenance failed, naming what disagreed.
    Supervised: back to the approval queue ('held'). Autonomous: declined with
    a recorded decision ('declined'). Caller commits."""
    from engine import autonomy, event_state
    uid = event["event_uid"]
    url = url or _provenance(event.get("extra_json")).get("url")
    note = NOTE_PREFIX + "; ".join(disagreed)
    if url and url not in note:
        note += f" — source_url {url}"
    _write(conn, uid, {"status": HELD, "disagreed": disagreed, "source_url": url,
                       "at": iso_utc_now()})
    if autonomy.is_autonomous():
        if event_state.decline(conn, uid, note[:1000], from_states=("approved",)):
            autonomy.record(conn, "event", uid, autonomy.Decision(
                "drop", "provenance-mismatch", {"disagreed": disagreed, "source_url": url}))
        return "declined"
    now = iso_utc_now()
    conn.execute(
        "UPDATE events SET state = 'metadata-extracted', approval_status = 'pending', "
        "  approval_note = ?, state_updated_at = ? WHERE event_uid = ? AND state = 'approved'",
        (note[:1000], now, uid))
    return "held"


def held(conn: sqlite3.Connection, since: str) -> list[dict]:
    """Events held for a source mismatch: every one still awaiting the
    operator, and those declined (autonomous) since `since`."""
    rows = conn.execute(
        "SELECT event_uid, COALESCE(title, name) AS title, COALESCE(date_local, date) AS date, "
        "  state, extra_json FROM events "
        "WHERE json_extract(extra_json, '$.provenance.status') = ? "
        "  AND (state = 'metadata-extracted' OR (state = 'declined' AND state_updated_at >= ?)) "
        "ORDER BY date_utc, event_uid", (HELD, since)).fetchall()
    out = []
    for r in rows:
        prov = _provenance(r[4])
        out.append({"event_uid": r[0], "title": r[1], "date": r[2], "state": r[3],
                    "source_url": prov.get("source_url"),
                    "disagreed": prov.get("disagreed") or []})
    return out
