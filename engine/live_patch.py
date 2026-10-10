"""Safe edits to existing TownLoops events: full-body PATCH + GET verify.

TownLoops PATCH semantics (verified against the API source, townloops
`app/services/events.py:update_event`, and the live OpenAPI `EventPatch`):
per-field merge — an omitted/`null` field is left alone — except
`category_slugs` and `tags`, which REPLACE the whole set. Unknown keys such
as `venue_slug` are silently ignored, and a `venue_name` that doesn't
resolve CLEARS the venue. Full write-up:
`skills/business/townloops-api/references/event-patch-semantics.md`.

So a PATCH is never built from local fields alone: it starts from the
current live row (every round-trippable field, full category/tag sets),
applies the local changes on top, and a follow-up GET checks that what we
sent is what landed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# EventPatch fields the event GET resource also exposes, so they can
# round-trip, with the max length the API accepts for strings. Not
# round-tripped (merge leaves them alone; sent only as overrides):
# `venue_id` — PATCH resolves ids in-town only, so resending an event's
# cross-town venue (e.g. a Reno venue) would clear it; `venue_name`
# (write-only); `business_id` (not in the GET resource).
_ROUND_TRIP = {
    "title": (3, 200), "description": (0, 5000), "start_at": None, "end_at": None,
    "category_slugs": None, "source_url": (0, 500), "is_featured": None,
    "accessibility_notes": (0, 2000), "tags": None,
}
_SETS = ("category_slugs", "tags")
_TIMES = ("start_at", "end_at")


@dataclass
class PatchResult:
    body: dict[str, Any]
    mismatches: list[str] = field(default_factory=list)


def build_event_patch(current: dict, overrides: dict) -> dict[str, Any]:
    """Full PATCH body: every patchable field of the live row `current`,
    with `overrides` (the local changes) applied on top. Overrides set to
    None are ignored — None can't clear a field over PATCH anyway. A live
    value the API would reject (e.g. an already-blanked title) is left out
    rather than resent. Raises ValueError on an empty live row or a time
    override without a UTC offset (the API would read it as town-local)."""
    if not current.get("slug"):
        raise ValueError("no live row to build a PATCH from")
    changes = {k: v for k, v in overrides.items() if v is not None}
    for k in _TIMES:
        if k in changes and not _has_offset(changes[k]):
            raise ValueError(f"{k} {changes[k]!r} has no UTC offset")
    body: dict[str, Any] = {}
    for k, limits in _ROUND_TRIP.items():
        if k not in current:
            continue
        v = current[k]
        if k in _SETS:
            body[k] = list(v or [])
        elif v is not None and (not limits or limits[0] <= len(v) <= limits[1]):
            body[k] = v
    body.update(changes)
    start, end = _instant(body.get("start_at")), _instant(body.get("end_at"))
    if start and end and end <= start:
        # The API doesn't validate end vs start on PATCH; never send it.
        body.pop("end_at")
    return body


def verify_event(row: dict, body: dict) -> list[str]:
    """Fields of `body` that the live `row` doesn't reflect, plus an end time
    left before the start (PATCH can't clear one). `venue_name` is
    write-only, so only a sent `venue_id` is checked."""
    out = []
    for k, sent in body.items():
        if k in ("venue_name", "business_id"):
            continue
        got = row.get(k)
        if k in _SETS:
            same = sorted(sent or []) == sorted(got or [])
        elif k in _TIMES:
            same = _instant(sent) == _instant(got)
        else:
            same = sent == got
        if not same:
            out.append(f"{k}: sent {sent!r}, live has {got!r}")
    start, end = _instant(row.get("start_at")), _instant(row.get("end_at"))
    if start and end and end <= start:
        out.append(f"end_at: live end {row.get('end_at')!r} is not after start {row.get('start_at')!r}")
    return out


def apply_event_patch(client, slug: str, overrides: dict,
                      current: Optional[dict] = None) -> PatchResult:
    """GET (unless `current` is given) → full-body PATCH → GET verify.
    API errors and an unusable live row raise; a landed-but-wrong PATCH
    comes back as `mismatches` for the caller to flag."""
    if current is None:
        current = client.get("events", slug) or {}
    body = build_event_patch(current, overrides)
    client.update("events", slug, body)
    after = client.get("events", slug) or {}
    return PatchResult(body=body, mismatches=verify_event(after, body))


def _has_offset(value: Any) -> bool:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _instant(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- Entities (venues, businesses, communities, performers, creators) -------
#
# Entity PATCH is a plain per-field merge (townloops `update_venue` & co.):
# `None` leaves a field alone, an empty `name` is ignored, but any other
# value — `""` included — overwrites, and a `category_slug` that doesn't
# resolve CLEARS the category. There are no replace-the-set fields, so the
# full body is the live row's own values plus the overrides; resending a
# live value is a no-op.

# Patchable fields per resource, with the max length the API accepts.
ENTITY_FIELDS: dict[str, dict[str, int]] = {
    "venues": {"name": 160, "description": 5000, "address": 255, "website": 500, "phone": 60,
               "accessibility_notes": 5000, "image_path": 500, "image_alt": 255,
               "category_slug": 140},
    "businesses": {"name": 160, "description": 5000, "address": 255, "website": 500,
                   "phone": 60, "image_path": 500, "image_alt": 255, "instagram": 500,
                   "facebook": 500, "tiktok": 500, "twitter": 500, "youtube": 500,
                   "linkedin": 500, "category_slug": 140},
    "communities": {"name": 160, "kind": 32, "description": 5000, "website": 500,
                    "meeting_info": 5000, "instagram": 500, "facebook": 500, "tiktok": 500,
                    "twitter": 500, "youtube": 500, "linkedin": 500, "image_path": 500,
                    "image_alt": 255, "category_slug": 140},
    "performers": {"name": 160, "kind": 32, "description": 5000, "website": 500,
                   "instagram": 500, "spotify_url": 500, "facebook": 500, "tiktok": 500,
                   "twitter": 500, "youtube": 500, "linkedin": 500, "image_path": 500,
                   "image_alt": 255},
    "creators": {"name": 160, "kind": 32, "bio": 5000, "website": 500, "instagram": 500,
                 "youtube_url": 500, "facebook": 500, "tiktok": 500, "twitter": 500,
                 "linkedin": 500, "image_path": 500, "image_alt": 255},
}


def sendable(resource: str, key: str, value: Any) -> bool:
    """A non-blank string the API accepts for `key` (name needs 2+ chars)."""
    limit = ENTITY_FIELDS[resource].get(key)
    if limit is None or not isinstance(value, str) or not value.strip():
        return False
    return len(value) <= limit and (key != "name" or len(value) >= 2)


def build_entity_patch(resource: str, current: dict, overrides: dict) -> dict[str, Any]:
    """Full PATCH body: every patchable non-blank value of the live row
    `current`, with the non-blank `overrides` on top. Values the API would
    reject are left out, and blanks are never sent — `""` would clear."""
    if not current.get("slug"):
        raise ValueError("no live row to build a PATCH from")
    body = {k: v for k, v in current.items() if sendable(resource, k, v)}
    body.update({k: v for k, v in overrides.items() if sendable(resource, k, v)})
    return body


def verify_entity(row: dict, body: dict) -> list[str]:
    """Fields of `body` that the live `row` doesn't reflect."""
    return [f"{k}: sent {v!r}, live has {row.get(k)!r}"
            for k, v in body.items() if row.get(k) != v]


def apply_entity_patch(client, resource: str, slug: str, overrides: dict,
                       current: Optional[dict] = None) -> PatchResult:
    """GET (unless `current` is given) → full-body PATCH → GET verify. API
    errors raise (a 404 as the client's error with status 404)."""
    if current is None:
        current = client.get(resource, slug) or {}
    body = build_entity_patch(resource, current, overrides)
    client.update(resource, slug, body)
    after = client.get(resource, slug) or {}
    return PatchResult(body=body, mismatches=verify_entity(after, body))


def shows(row: dict, field: str, value: Any) -> bool:
    """Whether the live `row` already carries `value` for `field`. Sets
    compare unordered, times as instants; `venue_name` is write-only on
    events, so it shows only where the row names a matching venue."""
    if field == "venue_name":
        from engine.derive_helpers import names_match
        return names_match(value, row.get("venue_name"))
    return not verify_event({field: row.get(field)}, {field: value})
