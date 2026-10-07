"""Fake TownLoops API mirroring the event PATCH contract (townloops repo,
`app/schemas/api.py:EventPatch` + `app/services/events.py:update_event` /
`_resolve_venue`): per-field merge with `None` = leave alone,
`category_slugs`/`tags` replace the whole set, `venue_id` wins over
`venue_name` and resolves in-town only, a `venue_name` resolves in-town then
globally and clears the venue when unresolved, a title under 3 chars is a
422, and unknown keys (e.g. `venue_slug`) are silently ignored.

`create` mirrors `EventPayload` + `EventResource`: tags come back as slugs,
both sets sorted. `create_drops` names fields a create path loses (the CMO
saw bulk create drop `tags`).
"""

from __future__ import annotations

import copy
import re


EVENT_PATCH_FIELDS = {
    "title", "description", "start_at", "end_at", "venue_name", "venue_id",
    "category_slugs", "business_id", "source_url", "is_featured",
    "accessibility_notes", "tags",
}


class FakeAPI:
    def __init__(self, events=None, venues=None, ignore=(), create_drops=()):
        self.events = {e["slug"]: copy.deepcopy(e) for e in (events or [])}
        self.venues = {v["slug"]: dict(v) for v in (venues or [])}
        self.ignore = set(ignore)  # fields the "server" drops — forces a verify mismatch
        self.create_drops = set(create_drops)
        self.calls: list[tuple] = []

    def get(self, resource, slug):
        self.calls.append(("GET", resource, slug))
        table = self.events if resource == "events" else self.venues
        return copy.deepcopy(table[slug])

    def create(self, resource, payload, idempotency_key=None):
        self.calls.append(("POST", resource, copy.deepcopy(payload)))
        row = {"id": len(self.events) + 1, "slug": payload["slug"], "town_slug": "slt",
               "venue_id": None, "venue_slug": None, "category_slugs": [], "tags": []}
        for k in EVENT_PATCH_FIELDS - {"venue_id", "venue_name"}:
            if k in payload and k not in self.create_drops:
                row[k] = payload[k]
        row["category_slugs"] = sorted(row["category_slugs"])
        row["tags"] = sorted(_slug(t) for t in row["tags"])
        self.events[row["slug"]] = row
        return copy.deepcopy(row)

    def update(self, resource, slug, payload):
        self.calls.append(("PATCH", resource, slug, copy.deepcopy(payload)))
        row = self.events[slug]
        if "title" in payload and payload["title"] is not None and len(payload["title"]) < 3:
            raise RuntimeError("[422 validation_error] title too short")
        body = {k: v for k, v in payload.items()
                if k in EVENT_PATCH_FIELDS and k not in self.ignore and v is not None}
        venue_id, venue_name = body.pop("venue_id", None), body.pop("venue_name", None)
        if venue_id is not None or venue_name:
            if venue_id is not None:
                v = next((v for v in self.venues.values()
                          if v["id"] == venue_id and v.get("town", "slt") == row.get("town_slug", "slt")), None)
            else:
                v = next((v for v in self.venues.values()
                          if v["name"].lower() == venue_name.lower()), None)
            row["venue_id"] = v["id"] if v else None
            row["venue_slug"] = v["slug"] if v else None
        for k in ("category_slugs", "tags"):
            if k in body:
                row[k] = sorted(body.pop(k) if k == "category_slugs"
                                else (_slug(t) for t in body.pop(k)))
        row.update(body)
        return copy.deepcopy(row)

    def patches(self):
        return [c[3] for c in self.calls if c[0] == "PATCH"]

    def creates(self):
        return [c[2] for c in self.calls if c[0] == "POST"]


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
