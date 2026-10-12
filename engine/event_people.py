"""Who is behind an event: its hosting organisation (business, community
group or non-profit) and its named performers and creators, read from the
event's own text by one quote-checked LLM pass.

The LLM may only copy from the page — the event's title and description
plus its detail page, when event-read-detail read one
(`event_detail.cached_page`). A name is kept only if it appears on that text
(engine/page_extract.normalize), and a host's kind only if a quote from the
text supports it (or the name itself says it: "… Foundation", "… Club",
"… LLC"). Anything unverifiable is NULL — no host, no performer — never a
guess. The answer is cached on the event (`extra_json.people`), so
event-derive-hosts and event-derive-people share one call.

Identity is not decided here: a performer or creator is only a name for
the performer-research batch to resolve (engine/performer_research).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

from engine.derive_helpers import names_match, normalize_name, parse_extra_json
from engine.event_extract import codex_exec
from engine.page_extract import find_quote, normalize, parse_object


HOST_KINDS = ("business", "community", "nonprofit")
MAX_PEOPLE = 5
TEXT_CHARS = 12000

_NONPROFIT = re.compile(r"\b(?:non-?profit|501\s?\(?c\)?\s?\(?3\)?|foundation|charity|charitable)\b",
                        re.IGNORECASE)
_COMMUNITY = re.compile(r"\b(?:club|association|society|council|committee|league|guild|"
                        r"church|parish|congregation|coalition|alliance|hoa|rotary|lions|"
                        r"neighborhood|friends of)\b", re.IGNORECASE)
_BUSINESS = re.compile(r"\b(?:llc|inc|co\.|company|brewing|brewery|restaurant|cafe|café|bar|"
                       r"grill|tavern|shop|store|gallery|studio|hotel|resort|casino|winery)\b",
                       re.IGNORECASE)


def _kind_from_words(text: str) -> Optional[str]:
    for kind, pattern in (("nonprofit", _NONPROFIT), ("community", _COMMUNITY),
                          ("business", _BUSINESS)):
        if pattern.search(text or ""):
            return kind
    return None


def host_venue(conn: sqlite3.Connection, name: str, event: dict,
               live_venues) -> Optional[str]:
    """The venue a host name really is, as a log phrase, or None. A venue is
    never an event's host business or community — the venue link carries it.

    Checked in order — the event's own venue, every local venue, then the
    live TownLoops venues (`live_venues()`, fetched only if needed) — with
    the shared entity-name match (derive_helpers.names_match), so a suffix
    variant ("Cascade Kitchens Food Hall & Bar") is its venue. A name that
    adds a club/non-profit word to a venue's ("… College Foundation") is an
    organisation of its own."""
    def same(venue_name: Optional[str]) -> bool:
        if not names_match(name, venue_name):
            return False
        extra = set(normalize_name(name).split()) - set(normalize_name(venue_name).split())
        return _kind_from_words(" ".join(extra)) not in ("community", "nonprofit")

    own = [event.get("venue_raw"), event.get("venue_name")]
    if event.get("derived_venue_uid"):
        row = conn.execute("SELECT name FROM venues WHERE entity_uid = ?",
                           (event["derived_venue_uid"],)).fetchone()
        own.append(row[0] if row else None)
    for v in own:
        if v and same(v):
            return f"the event's own venue '{v}'"
    for v_name, slug in conn.execute("SELECT name, townloops_slug FROM venues").fetchall():
        if same(v_name):
            return f"local venue '{v_name}'" + (f" ({slug})" if slug else "")
    for v in live_venues():
        if same(v.get("name")):
            return f"live venue {v.get('slug')} '{v.get('name')}'"
    return None


def event_text(event: dict, page: Optional[str]) -> str:
    parts = [event.get("title") or event.get("name") or "", event.get("description") or ""]
    if page:
        parts.append(page)
    return "\n\n".join(p for p in parts if p)[:TEXT_CHARS]


def build_prompt(event: dict, text: str) -> str:
    return f"""Below is the text about the event "{event.get('title') or event.get('name')}".

Return ONLY a JSON object — no prose, no markdown, no code fence — with these keys:
  "host": the organisation that puts on this event, or null. An object with
     "name": its name copied exactly from the text,
     "kind": "business", "community" (a club, group, association, church…) or "nonprofit",
     "kind_quote": words copied exactly from the text that show what kind of organisation it is, or null.
     The venue that merely hosts the room is not the host unless the text says it organises the event.
  "people": JSON array of the named performers (bands, musicians, speakers, comedians, instructors)
     and creators (artists, authors) appearing at this event — each {{"name": copied exactly,
     "role": "performer" or "creator"}}. [] if none are named.
Never infer a name or a kind that the text does not state.

Text follows:
---
{text}
---
"""


def _on_text(value, norm_text: str, min_len: int = 3) -> Optional[str]:
    found = find_quote(value, norm_text, min_len)
    return found.text if found else None


def verify(parsed: dict, text: str, event: dict) -> dict:
    """Only what the text bears out: {host: {name, kind} | None,
    people: [{name, role}]}."""
    norm = normalize(text)
    venue = normalize(event.get("venue_name") or event.get("venue_raw") or "")
    host = None
    h = parsed.get("host") if isinstance(parsed.get("host"), dict) else None
    if h:
        name = _on_text(h.get("name"), norm)
        kind = None
        if name and h.get("kind") in HOST_KINDS and _on_text(h.get("kind_quote"), norm, 4):
            kind = h["kind"]
        kind = kind or (_kind_from_words(name) if name else None)
        if name and kind:
            host = {"name": name[:160], "kind": kind}
    people = []
    seen = set()
    for p in parsed.get("people") or []:
        if not isinstance(p, dict) or p.get("role") not in ("performer", "creator"):
            continue
        name = _on_text(p.get("name"), norm)
        key = normalize(name or "")
        if not name or len(name) > 80 or key in seen or key == venue \
                or (host and key == normalize(host["name"])):
            continue
        seen.add(key)
        people.append({"name": name, "role": p["role"]})
    return {"host": host, "people": people[:MAX_PEOPLE]}


def derive(conn: sqlite3.Connection, event: dict, page: Optional[str],
           llm=None) -> Optional[dict]:
    """The event's verified host and people — cached on the event after the
    first successful call. None when the LLM call failed (not looked at)."""
    cached = parse_extra_json(event.get("extra_json")).get("people")
    if isinstance(cached, dict):
        return cached
    llm = llm or codex_exec
    text = event_text(event, page)
    ok, out, err = llm(build_prompt(event, text), timeout_s=60)
    parsed = parse_object(out) if ok else None
    if parsed is None:
        return None
    result = verify(parsed, text, event)
    extra = parse_extra_json(event.get("extra_json"))
    extra["people"] = result
    conn.execute("UPDATE events SET extra_json = ? WHERE event_uid = ?",
                 (json.dumps(extra), event["event_uid"]))
    return result
