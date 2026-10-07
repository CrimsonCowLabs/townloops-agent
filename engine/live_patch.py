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
