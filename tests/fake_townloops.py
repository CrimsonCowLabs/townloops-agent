"""Fake TownLoops API mirroring the event PATCH contract (townloops repo,
`app/schemas/api.py:EventPatch` + `app/services/events.py:update_event` /
`_resolve_venue`): per-field merge with `None` = leave alone,
`category_slugs`/`tags` replace the whole set, `venue_id` wins over
`venue_name` and resolves in-town only, a `venue_name` resolves in-town then
globally and clears the venue when unresolved, a title under 3 chars is a
422, and unknown keys (e.g. `venue_slug`) are silently ignored.

`create` mirrors `EventPayload` + `EventResource`: tags come back as slugs,
both sets sorted, the venue bound like PATCH does; a duplicate slug is a
409. `create_drops` names fields a create path loses (the CMO saw bulk
create drop `tags`). A `venue_name` naming several in-town venues fails the
way the server's `scalar_one_or_none` does ("Multiple rows were found").
A missing slug (or another town's) GETs as a 404; `list("events")` honours `from`/`to`.

Entity PATCH (venues, businesses, ...) mirrors `update_venue` & co.: a
per-field merge where `None` leaves a field alone, an empty `name` is
ignored, any other value — `""` included — is written, a `category_slug`
not in `categories` (when given) clears the category, and a missing slug
is a 404.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone

from engine.townloops_api import TownloopsError


EVENT_PATCH_FIELDS = {
    "title", "description", "start_at", "end_at", "venue_name", "venue_id",
    "category_slugs", "business_id", "source_url", "is_featured",
    "accessibility_notes", "tags",
}


class FakeAPI:
    def __init__(self, events=None, venues=None, ignore=(), create_drops=(),
                 entities=None, categories=None):
        self.events = {e["slug"]: copy.deepcopy(e) for e in (events or [])}
        self.venues = {v["slug"]: dict(v) for v in (venues or [])}
        self.tables = {"events": self.events, "venues": self.venues}
        for resource, rows in (entities or {}).items():
            self.tables.setdefault(resource, {}).update({r["slug"]: dict(r) for r in rows})
        self.categories = set(categories) if categories is not None else None
        self.ignore = set(ignore)  # fields the "server" drops — forces a verify mismatch
        self.create_drops = set(create_drops)
        self.calls: list[tuple] = []

    def verify_town(self, *a, **k):
        return {"town_slug": "slt"}

    def get(self, resource, slug):
        self.calls.append(("GET", resource, slug))
        table = self.tables.setdefault(resource, {})
        # Town-scoped like the server: another town's venue is a 404 here.
        if slug not in table or table[slug].get("town", "slt") != "slt":
            raise TownloopsError(404, "not_found", f"{resource} {slug!r} not found")
        return copy.deepcopy(table[slug])

    def list(self, resource, **params):
        self.calls.append(("LIST", resource, dict(params)))
        rows = list(self.tables.setdefault(resource, {}).values())
        lo, hi = _instant(params.get("from")), _instant(params.get("to"))
        if lo or hi:
            rows = [r for r in rows if (not lo or _instant(r["start_at"]) >= lo)
                    and (not hi or _instant(r["start_at"]) <= hi)]
        return copy.deepcopy(rows[params.get("offset", 0):][:params.get("limit", 50)])

    def paginate(self, resource, page_size=200, **params):
        offset = 0
        while True:
            page = self.list(resource, limit=page_size, offset=offset, **params)
            yield from page
            if len(page) < page_size:
                return
            offset += page_size

    def create(self, resource, payload, idempotency_key=None):
        self.calls.append(("POST", resource, copy.deepcopy(payload)))
        table = self.tables.setdefault(resource, {})
        if payload["slug"] in table:
            raise TownloopsError(409, "slug_taken", f"slug {payload['slug']!r} taken")
        if resource != "events":
            row = {**copy.deepcopy(payload), "id": 1000 + len(table)}
            table[row["slug"]] = row
            return copy.deepcopy(row)
        row = {"id": len(self.events) + 1, "slug": payload["slug"], "town_slug": "slt",
               "venue_id": None, "venue_slug": None, "category_slugs": [], "tags": []}
        for k in EVENT_PATCH_FIELDS - {"venue_id", "venue_name"}:
            if k in payload and k not in self.create_drops:
                row[k] = payload[k]
        self._bind_venue(row, payload.get("venue_id"), payload.get("venue_name"))
        row["category_slugs"] = sorted(row["category_slugs"])
        row["tags"] = sorted(_slug(t) for t in row["tags"])
        self.events[row["slug"]] = row
        return copy.deepcopy(row)

    def _bind_venue(self, row, venue_id, venue_name):
        if venue_id is None and not venue_name:
            return
        town = row.get("town_slug", "slt")
        if venue_id is not None:
            v = next((v for v in self.venues.values()
                      if v["id"] == venue_id and v.get("town", "slt") == town), None)
        else:
            in_town = [v for v in self.venues.values()
                       if v["name"].lower() == venue_name.lower() and v.get("town", "slt") == town]
            if len(in_town) > 1:
                raise TownloopsError(500, "internal_error",
                                     "Multiple rows were found when one or none was required")
            v = in_town[0] if in_town else next(
                (v for v in self.venues.values() if v["name"].lower() == venue_name.lower()), None)
        row["venue_id"] = v["id"] if v else None
        row["venue_slug"] = v["slug"] if v else None

    def update(self, resource, slug, payload):
        self.calls.append(("PATCH", resource, slug, copy.deepcopy(payload)))
        if resource != "events":
            return self._update_entity(resource, slug, payload)
        row = self.events[slug]
        if "title" in payload and payload["title"] is not None and len(payload["title"]) < 3:
            raise RuntimeError("[422 validation_error] title too short")
        body = {k: v for k, v in payload.items()
                if k in EVENT_PATCH_FIELDS and k not in self.ignore and v is not None}
        self._bind_venue(row, body.pop("venue_id", None), body.pop("venue_name", None))
        for k in ("category_slugs", "tags"):
            if k in body:
                row[k] = sorted(body.pop(k) if k == "category_slugs"
                                else (_slug(t) for t in body.pop(k)))
        row.update(body)
        return copy.deepcopy(row)

    def _update_entity(self, resource, slug, payload):
        table = self.tables.setdefault(resource, {})
        if slug not in table or table[slug].get("town", "slt") != "slt":
            raise TownloopsError(404, "not_found", f"{resource} {slug!r} not found")
        row = table[slug]
        for k, v in payload.items():
            if v is None or k in self.ignore or (k == "name" and v == ""):
                continue
            if k == "category_slug" and self.categories is not None and v not in self.categories:
                v = None
            row[k] = v
        return copy.deepcopy(row)

    def patches(self):
        return [c[3] for c in self.calls if c[0] == "PATCH"]

    def creates(self):
        return [c[2] for c in self.calls if c[0] == "POST"]


def _instant(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
