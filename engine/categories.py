"""Event categories and tags, in the shape the TownLoops API takes them.

Categories are the town's curated live taxonomy (`state/categories.json`,
snapshotted by `town-init setup`), several per event, sent as
`category_slugs[]` — the event create schema has no singular `category_slug`.
Whatever the extractor or an old row says is mapped onto those slugs; a value
that maps to none (or to more than one) is dropped rather than shipped.

Tags are free labels. The API slugifies them and returns slugs, so they are
kept as slugs locally too — a GET-verify then compares like with like.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional

from engine.policy import categories_snapshot_path

# EventPayload / EventPatch list limits.
MAX_CATEGORIES = 6
MAX_TAGS = 10
_TAG_MAX_LEN = 60


def town_categories(snapshot_path: Optional[Path] = None) -> list[dict]:
    """The town's live categories (`slug`, `name`, `description`, …) from the
    snapshot. Empty when the snapshot is missing or unreadable."""
    path = snapshot_path or categories_snapshot_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("categories") or []
    return [c for c in data if isinstance(c, dict) and isinstance(c.get("slug"), str)]


def town_category_slugs(snapshot_path: Optional[Path] = None) -> set[str]:
    return {c["slug"] for c in town_categories(snapshot_path)}


def slugify(value: str) -> str:
    """ASCII, lowercase, hyphen-joined — the API's tag slug form."""
    ascii_ = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:_TAG_MAX_LEN]


def _split(values: Any) -> list[str]:
    """A list, a comma-separated string, or a JSON-array string → items."""
    if isinstance(values, str):
        text = values.strip()
        if text.startswith("["):
            try:
                return _split(json.loads(text))
            except json.JSONDecodeError:
                pass
        return text.split(",")
    if isinstance(values, (list, tuple)):
        return [p for v in values if isinstance(v, str) for p in v.split(",")]
    return []


def _words(slug: str) -> frozenset[str]:
    """Words of a slug, singular — so `art` meets `arts` and `music` meets
    `live-music`. `and` is noise (`food-and-drink`)."""
    return frozenset(w[:-1] if len(w) > 3 and w.endswith("s") else w
                     for w in slug.split("-") if w and w != "and")


def _match(value: str, allowed: set[str]) -> Optional[str]:
    slug = slugify(value)
    if not slug:
        return None
    if slug in allowed:
        return slug
    words = _words(slug)
    hits = [a for a in allowed if words and (words <= _words(a) or _words(a) <= words)]
    return hits[0] if len(hits) == 1 else None


def normalize_category_slugs(values: Any, allowed: Iterable[str]) -> list[str]:
    """Map extracted/stored categories onto the town's live slugs, in order,
    deduped, capped at the API maximum. Unknown or ambiguous values are
    dropped."""
    allowed = set(allowed)
    out: list[str] = []
    for v in _split(values):
        slug = _match(v, allowed)
        if slug and slug not in out:
            out.append(slug)
    return out[:MAX_CATEGORIES]


def normalize_tags(values: Any) -> list[str]:
    """Free tags as API slugs, in order, deduped, capped at the API maximum."""
    out: list[str] = []
    for v in _split(values):
        slug = slugify(v)
        if slug and slug not in out:
            out.append(slug)
    return out[:MAX_TAGS]


def classify(categories: Any, tags: Any) -> tuple[list[str], list[str]]:
    """(category_slugs, tags) ready to store or send: categories mapped onto
    the town's live slugs, tags as slugs."""
    return (normalize_category_slugs(categories, town_category_slugs()),
            normalize_tags(tags))


def prompt_vocabulary(categories: list[dict]) -> str:
    """One `slug (Name: description)` per line for the extraction prompt."""
    return "\n".join(
        f"      {c['slug']} ({c.get('name') or c['slug']}"
        + (f": {c['description']}" if c.get("description") else "") + ")"
        for c in categories
    )


def to_json(slugs: list[str]) -> Optional[str]:
    """DB form: a JSON array, or NULL for none."""
    return json.dumps(slugs) if slugs else None
