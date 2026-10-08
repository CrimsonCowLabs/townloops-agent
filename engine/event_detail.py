"""One budgeted read of an event's own detail page (its `source_url`).

event-find and event-reconcile only see the seed's listing page. After the
operator approves an event, the `read-detail` workqueue action (skill
`event-read-detail`) reads the event's own page once — Firecrawl markdown +
one Codex call — and stores what it states. Today that is accessibility
notes; ticket/price info rides the same pass (add a key to DETAIL_FIELDS and
a column to `_extract`).

Accessibility is a differentiator, but a wrong claim does harm. The LLM only
picks sentences; a sentence is kept only if it is on the page (quote check)
and is about access (wheelchair, ASL, sensory-friendly, …) — "accessible by
trail", "all welcome" or a site's "digital accessibility" footer never are.
Nothing is inferred: no quote → NULL.

The read is recorded on the event (`detail_read_*`), so it happens once per
source_url, not every tick; a page that is just the seed's listing (or no
source_url) is never fetched.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Optional

from engine.derive_helpers import iso_utc_now
from engine.discovery import canonicalize_url
from engine.event_extract import codex_exec, firecrawl_markdown


# EventPayload / EventPatch limit.
ACCESSIBILITY_MAX = 2000
# One read must finish inside the orchestrator's 120s per-skill timeout, or
# the kill (rc 124) skips the give-up path and the event ends up stuck:
# ≤30s Firecrawl token-bucket wait + ≤30s scrape (+3.5s waitFor) + ≤40s Codex.
FETCH_TIMEOUT_S = 25
CODEX_TIMEOUT_S = 40
PAGE_CHARS = 12000

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
_MIN_QUOTE_CHARS = 12


@dataclass
class DetailResult:
    status: str                  # read | no-detail-page | already-read | retry | failed
    fields: dict = field(default_factory=dict)
    error: str = ""


# --------------------------------------------------------------------------
# Quote checks
# --------------------------------------------------------------------------

_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_EMPHASIS = re.compile(r"[*_`]+")
_MD_BLOCKS = re.compile(r"[#>|]+")
_DASHES = str.maketrans({"—": "-", "–": "-", "‘": "'", "’": "'",
                         "“": '"', "”": '"', " ": " "})


def _norm(text: str) -> str:
    """Page and quote text as compared: markdown links → their text, emphasis
    and heading marks gone, dashes/quotes unified, whitespace collapsed."""
    text = _MD_LINK.sub(r"\1", text.translate(_DASHES))
    text = _MD_BLOCKS.sub(" ", _MD_EMPHASIS.sub("", text))
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _about_access(quote: str) -> bool:
    return bool(_ACCESS_TERMS.search(quote) or _ADA.search(quote))


def accessibility_notes(quotes, page: str) -> Optional[str]:
    """The quotes that are on `page` and about access, in page order, as
    sentences joined by spaces and cut on a quote boundary to the API limit.
    None when none survive."""
    if not isinstance(quotes, list):
        return None
    norm_page = _norm(page)
    kept: dict[str, tuple[int, str]] = {}
    for q in quotes:
        if not isinstance(q, str):
            continue
        quote = re.sub(r"\s+", " ", q).strip().strip("\"'")
        key = _norm(quote).rstrip(".,;:!? ")
        if len(key) < _MIN_QUOTE_CHARS or key in kept or not _about_access(quote):
            continue
        pos = norm_page.find(key)
        if pos < 0:
            continue
        kept[key] = (pos, quote if quote[-1] in ".!?" else quote.rstrip(",;:") + ".")
    notes = ""
    for _, sentence in sorted(kept.values()):
        joined = f"{notes} {sentence}".strip()
        if len(joined) > ACCESSIBILITY_MAX:
            break
        notes = joined
    return notes or None


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


def _parse_object(stdout: str) -> Optional[dict]:
    text = (stdout or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _extract(parsed: dict, page: str) -> dict:
    """Event columns from the LLM's answer, each checked against the page."""
    return {"accessibility_notes": accessibility_notes(parsed.get("accessibility_quotes"), page)}


def _record(conn, event: dict, status: str, url: Optional[str], fields: dict) -> None:
    # A NULL never blanks a value an earlier read stored.
    sets = ", ".join(f"{k} = COALESCE(?, {k})" for k in fields)
    conn.execute(
        "UPDATE events SET detail_read_status = ?, detail_read_url = ?, detail_read_at = ?"
        + (f", {sets}" if sets else "") + " WHERE event_uid = ?",
        (status, url, iso_utc_now(), *fields.values(), event["event_uid"]),
    )


def read_detail(conn: sqlite3.Connection, event: dict, *, final_attempt: bool,
                fetch: Optional[Callable] = None, llm: Optional[Callable] = None) -> DetailResult:
    """Read `event`'s detail page once and store what it states. A failed
    fetch or LLM call is `retry` (nothing recorded) unless `final_attempt`,
    when it is recorded as `failed` so the event can go on without it."""
    fetch = fetch or (lambda u: firecrawl_markdown(u, timeout_s=FETCH_TIMEOUT_S))
    llm = llm or codex_exec
    url = event.get("source_url")
    canon = _canon(url)
    if not canon or canon in seed_urls(conn, event):
        _record(conn, event, "no-detail-page", url, {})
        return DetailResult("no-detail-page")
    if event.get("detail_read_status") == "read" and event.get("detail_read_url") == url:
        return DetailResult("already-read")

    try:
        ok, markdown, err = fetch(url)
    except TimeoutError as e:  # the Firecrawl token bucket stayed empty
        ok, markdown, err = False, "", str(e)
    if ok:
        ok, out, err = llm(build_detail_prompt(event, url, markdown), timeout_s=CODEX_TIMEOUT_S)
        parsed = _parse_object(out) if ok else None
        if ok and parsed is None:
            ok, err = False, f"no JSON object in Codex output: {out[:200]!r}"
    if not ok:
        if final_attempt:
            _record(conn, event, "failed", url, {})
            return DetailResult("failed", error=err)
        return DetailResult("retry", error=err)
    fields = _extract(parsed, markdown)
    _record(conn, event, "read", url, fields)
    return DetailResult("read", fields=fields)
