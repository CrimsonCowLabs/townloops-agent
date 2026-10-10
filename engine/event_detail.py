"""One budgeted read of an event's own detail page (its `source_url`).

event-find and event-reconcile only see the seed's listing page. After the
operator approves an event, the `read-detail` workqueue action (skill
`event-read-detail`) reads the event's own page once — Firecrawl markdown +
one Codex call — and stores what it states: accessibility notes, and the
price and ticket/RSVP link (shipped via engine/event_tickets.py). A new
field is a key in DETAIL_FIELDS and a column in `_extract`.

Accessibility is a differentiator, but a wrong claim does harm. The LLM only
picks sentences; a sentence is kept only if it is on the page (quote check)
and is about access (wheelchair, ASL, sensory-friendly, …) — "accessible by
trail", "all welcome" or a site's "digital accessibility" footer never are.
Nothing is inferred: no quote → NULL. The same goes for a price: it is kept
only if the quote is on the page and states a cost; a ticket link only if
it is on the page and is not a homepage or the seed's listing page.

The read is recorded on the event (`detail_read_*`), so it happens once per
source_url, not every tick; a page that is just the seed's listing (or no
source_url) is never fetched. The page text is cached (`cached_page`) for
the host/people derivation (engine/event_people).

A page that itself says the event is cancelled or postponed
(autonomy.cancellation_evidence) ends the read there, without the Codex
call: the event is declined with the page's sentence as the reason
(engine/event_cancel.decline_cancelled), so it never ships.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlsplit

from engine import autonomy, event_cancel
from engine.derive_helpers import iso_utc_now
from engine.discovery import canonicalize_url
from engine.event_extract import codex_exec, firecrawl_markdown
from engine.event_tickets import price_kind
from engine.page_extract import join_quotes, normalize, parse_object


# EventPayload / EventPatch limit.
ACCESSIBILITY_MAX = 2000
# One read must finish inside the orchestrator's 120s per-skill timeout, or
# the kill (rc 124) skips the give-up path and the event ends up stuck:
# ≤30s Firecrawl token-bucket wait + ≤30s scrape (+3.5s waitFor) + ≤40s Codex.
FETCH_TIMEOUT_S = 25
CODEX_TIMEOUT_S = 40
PAGE_CHARS = 12000
PRICE_MAX = 300

# Keys asked of the LLM, one prompt line each.
DETAIL_FIELDS = {
    "accessibility_quotes": (
        "JSON array of sentences copied word for word from the page that state "
        "accessibility for this event or its venue: wheelchair/step-free access, "
        "accessible seating, parking or restrooms, ASL or captioning, assisted "
        "listening, sensory-friendly or relaxed sessions, service animals. "
        "Copy the page's exact words. Do not paraphrase, summarize or infer "
        "(\"all welcome\", \"accessible by trail\" and a site-wide accessibility "
        "statement are not accessibility info). [] if the page says nothing"
    ),
    "price_quote": (
        "the sentence or phrase copied word for word from the page that states "
        "what it costs to attend this event: a price or price range, free, sold "
        "out / waitlist, or a donation / pay-what-you-can. null if the page does "
        "not say. Never guess a price; free parking or free snacks do not make "
        "the event free"
    ),
    "ticket_url": (
        "the full http(s) URL, exactly as it appears on the page, of the link to "
        "buy tickets, RSVP or register for this event (a ticketing or "
        "registration page, or this event's own page). Never a site or venue "
        "homepage. null if there is none"
    ),
}

# What makes a sentence about access. A bare "accessible"/"accessibility" is
# not enough ("accessible by trail", "committed to digital accessibility").
_ACCESS_TERMS = re.compile(
    r"wheel\s?chair|step[- ]free|\bASL\b|sign[- ]language|caption|audio[- ]descri"
    r"|assist(?:ed|ive)[- ]listening|hearing[- ]loop|induction[- ]loop|braille"
    r"|large[- ]print|low[- ]vision|sensory|relaxed[- ]performance|autism"
    r"|quiet[- ](?:room|space|area)|service[- ](?:animal|dog)|mobility"
    r"|disab(?:led|ility|ilities)|handicap|companion[- ]seat|elevator"
    r"|accessible[- ](?:seat|parking|entr|restroom|bathroom|toilet|route|viewing|space)"
    r"|(?:ADA|wheelchair)[- ](?:accessible|compliant|seating|parking)",
    re.IGNORECASE,
)
_ADA = re.compile(r"\bADA\b")
_MD_EMPHASIS = re.compile(r"[*_`]+")


@dataclass
class DetailResult:
    status: str                  # read | no-detail-page | already-read | retry | failed | cancelled
    fields: dict = field(default_factory=dict)
    error: str = ""


# --------------------------------------------------------------------------
# Quote checks
# --------------------------------------------------------------------------

def _about_access(quote: str) -> bool:
    return bool(_ACCESS_TERMS.search(quote) or _ADA.search(quote))


def accessibility_notes(quotes, page: str) -> Optional[str]:
    """The quotes that are on `page` and about access, in page order, as
    sentences joined by spaces and cut on a quote boundary to the API limit.
    None when none survive."""
    return join_quotes(quotes, page, _about_access, ACCESSIBILITY_MAX)


def ticket_price(quote, page: str) -> Optional[str]:
    """The page's own price wording, or None unless it is on `page` and
    states a cost (engine/event_tickets.price_kind)."""
    if not isinstance(quote, str):
        return None
    text = re.sub(r"\s+", " ", _MD_EMPHASIS.sub("", quote)).strip().strip("\"'")
    key = normalize(text).rstrip(".,;:!? ")
    kind = price_kind(text)
    if not key or len(text) > PRICE_MAX or kind is None:
        return None
    norm_page = normalize(page)
    pos = norm_page.find(key)
    if pos < 0:
        return None
    # Read a fragment ("Free") to the end of its sentence on the page
    # ("free parking in lot"): it must still say the same thing.
    end = _SENTENCE_END.search(norm_page, pos + len(key))
    return text if price_kind(norm_page[pos:end.start() if end else None]) == kind else None


_SENTENCE_END = re.compile(r"[!?;]|\.(?!\d)")
_HOMEPAGE_PATH = re.compile(r"/?(?:(?:home|index|default)(?:\.\w+)?/?)?", re.IGNORECASE)
_PAGE_URL = re.compile(r"https?://[^\s)\]>\"'<]+")


def ticket_url(url, page: str, exclude: set[str]) -> Optional[str]:
    """`url` if it is an http(s) link on `page` that is not a site's
    homepage or one of `exclude` (canonical seed listing URLs), else None."""
    canon = _canon(url)
    if not canon or canon in exclude or _HOMEPAGE_PATH.fullmatch(urlsplit(canon).path):
        return None
    on_page = {_canon(u.rstrip(".,;:")) for u in _PAGE_URL.findall(page)}
    return url.strip() if canon in on_page else None


# --------------------------------------------------------------------------
# The read
# --------------------------------------------------------------------------

def _canon(url) -> Optional[str]:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    try:
        return canonicalize_url(url)
    except ValueError:
        return None


def seed_urls(conn: sqlite3.Connection, event: dict) -> set[str]:
    """Canonical listing URLs of the seed(s) the event was found on."""
    via = event.get("discovered_via") or event.get("source") or ""
    if not via.startswith("seed:"):
        return set()
    rows = conn.execute("SELECT url FROM event_seeds WHERE name = ?", (via[len("seed:"):],))
    return {u for u in (_canon(r[0]) for r in rows.fetchall()) if u}


def build_detail_prompt(event: dict, url: str, markdown: str) -> str:
    keys = "\n".join(f'  "{k}": {v}' for k, v in DETAIL_FIELDS.items())
    return f"""Below is the scraped markdown of the detail page ({url}) for the event "{event.get('title') or event.get('name')}".

Return ONLY a JSON object — no prose, no markdown, no code fence — with these keys:
{keys}

Scraped page follows:
---
{markdown[:PAGE_CHARS]}
---
"""


def _extract(parsed: dict, page: str, seeds: set[str]) -> dict:
    """Event columns from the LLM's answer, each checked against the page."""
    return {
        "accessibility_notes": accessibility_notes(parsed.get("accessibility_quotes"), page),
        "price": ticket_price(parsed.get("price_quote"), page),
        "rsvp_url": ticket_url(parsed.get("ticket_url"), page, seeds),
    }


def _record(conn, event: dict, status: str, url: Optional[str], fields: dict) -> None:
    # A NULL never blanks a value an earlier read stored.
    sets = ", ".join(f"{k} = COALESCE(?, {k})" for k in fields)
    conn.execute(
        "UPDATE events SET detail_read_status = ?, detail_read_url = ?, detail_read_at = ?"
        + (f", {sets}" if sets else "") + " WHERE event_uid = ?",
        (status, url, iso_utc_now(), *fields.values(), event["event_uid"]),
    )


def needs_fetch(conn: sqlite3.Connection, event: dict) -> bool:
    """Whether reading this event costs a scrape + an LLM call (it has a
    detail page of its own not read yet) — the rest finish at once."""
    canon = _canon(event.get("source_url"))
    if not canon or canon in seed_urls(conn, event):
        return False
    return not (event.get("detail_read_status") == "read"
                and event.get("detail_read_url") == event.get("source_url"))


def read_detail(conn: sqlite3.Connection, event: dict, *, final_attempt: bool,
                fetch: Optional[Callable] = None, llm: Optional[Callable] = None) -> DetailResult:
    """Read `event`'s detail page once and store what it states. A failed
    fetch or LLM call is `retry` (nothing recorded) unless `final_attempt`,
    when it is recorded as `failed` so the event can go on without it."""
    fetch = fetch or (lambda u: firecrawl_markdown(u, timeout_s=FETCH_TIMEOUT_S))
    llm = llm or codex_exec
    url = event.get("source_url")
    canon = _canon(url)
    seeds = seed_urls(conn, event)
    if not canon or canon in seeds:
        _record(conn, event, "no-detail-page", url, {})
        return DetailResult("no-detail-page")
    if event.get("detail_read_status") == "read" and event.get("detail_read_url") == url:
        return DetailResult("already-read")

    try:
        ok, markdown, err = fetch(url)
    except TimeoutError as e:  # the Firecrawl token bucket stayed empty
        ok, markdown, err = False, "", str(e)
    said = autonomy.cancellation_evidence(markdown, event.get("title") or event.get("name")) \
        if ok else None
    if said and said.verdict == "submit":
        quote = said.evidence["quote"]
        _record(conn, event, "read", url, {})
        event_cancel.decline_cancelled(conn, event["event_uid"], quote, "its own page",
                                       "page-states-cancelled")
        return DetailResult("cancelled", fields={"cancelled": quote})
    if ok:
        ok, out, err = llm(build_detail_prompt(event, url, markdown), timeout_s=CODEX_TIMEOUT_S)
        parsed = parse_object(out) if ok else None
        if ok and parsed is None:
            ok, err = False, f"no JSON object in Codex output: {out[:200]!r}"
    if not ok:
        if final_attempt:
            _record(conn, event, "failed", url, {})
            return DetailResult("failed", error=err)
        return DetailResult("retry", error=err)
    fields = _extract(parsed, markdown, seeds)
    _record(conn, event, "read", url, fields)
    cache_page(event["event_uid"], markdown)
    return DetailResult("read", fields=fields)


def _cache_dir():
    from engine import derive_helpers
    return derive_helpers._DB_PATH.parent / "cache" / "detail"


def cache_page(event_uid: str, markdown: str) -> None:
    """Keep the page text for the later derive steps (engine/event_people)."""
    try:
        d = _cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{event_uid}.md").write_text(markdown[:PAGE_CHARS])
    except OSError:
        pass


def drop_cached_page(event_uid: str) -> None:
    """The event shipped: its derive steps are done with the page."""
    try:
        (_cache_dir() / f"{event_uid}.md").unlink(missing_ok=True)
    except OSError:
        pass


def cached_page(event_uid: str) -> Optional[str]:
    """The event's detail page as read-detail saw it, if it read one."""
    try:
        return (_cache_dir() / f"{event_uid}.md").read_text()
    except OSError:
        return None
