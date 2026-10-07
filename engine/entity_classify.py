"""Entity category classifier — give a venue, business or community with no
category one from the town's live vocabulary (`state/categories.json`).

A keyword map decides the clear cases deterministically; when it is
inconclusive (no hint, or hints for more than one category) an optional LLM
fallback picks from the town's slugs and anything else it says is discarded.
Only a blank `category_slug` is ever filled, and live mirrors are left alone —
their values are TownLoops'.

Ported from the pre-rewrite entity-research skill (keyword map → Codex
fallback). The concept keys below are mapped onto each town's own slugs
(`food-and-drink` → `food-drink`, `outdoor-recreation` → `outdoors`), so one
map serves every town.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Callable, Iterable, Optional

from engine import categories as cats
from engine.derive_helpers import iso_utc_now
from engine.entity_sync import refresh_content_hash

# Prompt → reply text, or None when the call failed or wasn't made.
LLM = Callable[[str], Optional[str]]

# Kinds whose TownLoops payload has a `category_slug`.
TABLES = {"venue": "venues", "business": "businesses", "community": "communities"}

# Concept → whole-word hints in an entity's name + description. A hint for a
# concept the town has no slug for still counts — "Ski Resort" in a town
# without `lodging` isn't clearly `outdoors`.
_HINTS: dict[str, tuple[str, ...]] = {
    "food-and-drink": ("restaurant", "cafe", "café", "bakery", "brewery", "brewing", "coffee",
                       "bar", "pub", "eatery", "dining", "grill", "pizzeria", "pizza", "diner",
                       "bistro", "winery", "taproom", "distillery", "deli", "tavern", "saloon"),
    "live-music": ("live music", "concert", "concerts", "amphitheater", "amphitheatre",
                   "music venue", "nightclub", "jazz club"),
    "arts": ("gallery", "museum", "theater", "theatre", "art center", "arts center",
             "art studio", "playhouse"),
    "outdoor-recreation": ("outfitter", "outfitters", "kayak", "rafting", "ski", "snowboard",
                           "hiking", "climbing", "trail", "trails", "paddleboard", "paddle",
                           "beach", "marina", "campground", "golf"),
    "family": ("kids", "children", "playground", "arcade", "toy store"),
    "community": ("foundation", "nonprofit", "non-profit", "charity", "501(c)(3)", "library",
                  "church", "city of", "county of", "chamber of commerce", "city hall",
                  "rotary", "association", "volunteer", "volunteers"),
    "lodging": ("hotel", "motel", "inn", "lodge", "resort", "bed and breakfast",
                "vacation rental"),
    "shopping": ("shop", "store", "boutique", "outlet"),
    "wellness": ("spa", "salon", "yoga", "massage", "gym", "fitness"),
}

_HINT_RE = {concept: re.compile(r"(?<![\w'’-])(?:" + "|".join(re.escape(k) for k in kws) + r")(?![\w'’-])")
            for concept, kws in _HINTS.items()}


def keyword_category(name: str, description: str, allowed: Iterable[str]) -> Optional[str]:
    """The one town slug the text's hints point to, or None when no hint
    matches, hints for more than one category match, or the only match is a
    concept the town has no slug for."""
    allowed = set(allowed)
    text = f"{name} {description}".lower()
    hits: set[Optional[str]] = set()
    for concept, pattern in _HINT_RE.items():
        if pattern.search(text):
            slugs = cats.normalize_category_slugs([concept], allowed)
            hits.add(slugs[0] if slugs else None)
    return hits.pop() if len(hits) == 1 else None


def _prompt(name: str, description: str, url: str, categories: list[dict]) -> str:
    return (
        "You are a taxonomy classifier for a local-town directory. Pick the single "
        "best-fit category slug for this place or organization from the list below. "
        "Reply with just the slug on one line, or `none` if no category fits — no "
        "explanation, no markdown.\n\n"
        f"Categories:\n{cats.prompt_vocabulary(categories)}\n\n"
        f"Name: {name}\n"
        f"Description: {(description or '')[:400]}\n"
        f"URL: {url}\n\n"
        "Slug:"
    )


def _parse_slug(reply: str, allowed: set[str]) -> Optional[str]:
    """CLI output can carry agent noise before the answer: scan lines
    bottom-up for one whose first token is an allowed slug."""
    for line in reversed(reply.splitlines()):
        line = line.strip().strip("`*\"'•-").strip()
        if line.lower().startswith(("slug:", "answer:")):
            line = line.split(":", 1)[1].strip()
        token = line.split()[0].strip("`*\"'.,;:").lower() if line.split() else ""
        if token in allowed:
            return token
    return None


def llm_category(name: str, description: str, url: str, categories: list[dict],
                 llm: LLM) -> Optional[str]:
    """Ask `llm` to pick from `categories`; None unless it names one of them."""
    if not categories:
        return None
    reply = llm(_prompt(name, description, url, categories))
    if not reply:
        return None
    return _parse_slug(reply, {c["slug"] for c in categories})


def codex_llm(max_calls: int, timeout_s: int, deadline: float, run=None) -> LLM:
    """An LLM for llm_category backed by the Codex CLI (the repo's provider):
    at most `max_calls` calls, and none that could run past `deadline` (a
    time.monotonic() value), so a skill stays inside the orchestrator's
    timeout. A failed, throttled or skipped call returns None — the row
    stays blank and is retried the next time enrichment sees it."""
    if run is None:
        from engine.event_extract import codex_exec as run
    left = [max_calls]

    def call(prompt: str) -> Optional[str]:
        if left[0] <= 0 or time.monotonic() + timeout_s > deadline:
            return None
        left[0] -= 1
        ok, out, _err = run(prompt, timeout_s=timeout_s)
        return out if ok else None

    return call


def classify_entity(conn: sqlite3.Connection, kind: str, entity_uid: str,
                    llm: Optional[LLM] = None) -> Optional[str]:
    """Fill a blank `category_slug` on a local venue/business/community:
    keyword map first, then `llm` (if given) when the map is inconclusive.
    Returns the slug written, or None when nothing was written."""
    table = TABLES.get(kind)
    if not table:
        return None
    row = conn.execute(f"SELECT * FROM {table} WHERE entity_uid = ?", (entity_uid,)).fetchone()
    if not row or row["source_kind"] == "live" or (row["category_slug"] or "").strip():
        return None
    categories = cats.town_categories()
    allowed = {c["slug"] for c in categories}
    if not allowed:
        return None
    name, description = row["name"] or "", row["description"] or ""
    slug = keyword_category(name, description, allowed)
    if slug is None and llm is not None:
        slug = llm_category(name, description, row["website"] or "", categories, llm)
    if slug is None:
        return None
    cur = conn.execute(
        f"UPDATE {table} SET category_slug = ?, last_modified_at = ? "
        "WHERE entity_uid = ? AND (category_slug IS NULL OR trim(category_slug) = '')",
        (slug, iso_utc_now(), entity_uid),
    )
    if not cur.rowcount:
        return None
    refresh_content_hash(conn, kind, entity_uid)
    return slug
