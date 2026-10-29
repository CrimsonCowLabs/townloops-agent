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

Ingest Ref (townloops ADR 0011, #63): a create must not carry a `slug` (the
agent never proposes one — asserted here, for events and the five kinds); the
server derives it from the title/name, unique in the table. A create whose
`ingest_ref` a row in the town already holds — a deleted/merged one included —
is a `409 ingest_ref_taken` with `details = {ingest_ref, slug}` (the merge
survivor's slug when merged). `list(..., ingest_ref=…)` filters on it (deleted
and merged rows excluded). A PATCH may set `ingest_ref` once: a different ref
on a row that has one is a 422, a ref another row holds a 409. An event
create's `venue_ingest_ref` binds the venue after `venue_id`, before
`venue_name`. `rename` moves a row to a new slug (the old one 404s, no
survivor named).

Series (ADR 0007, townloops `app/services/series.py` + the ingest API's
`series` routes): every event carries `series_slug`/`series_id` (null for a
singleton); `create` auto-links to an *active* Series in the town whose title
has the event's `series_key`; `list("events", series=…)` is town-scoped and
`is_active`-agnostic (an unknown slug lists nothing); `list("series")` is the
town's active Series, `get("series", …)` any of them (404 cross-town); and a
write to `series` is refused — there is no write surface. `status=` filters
the event list.

Entity PATCH (venues, businesses, ...) mirrors `update_venue` & co.: a
per-field merge where `None` leaves a field alone, an empty `name` is
ignored, any other value — `""` included — is written, a `category_slug`
not in `categories` (when given) clears the category, and a missing slug
is a 404.

Moderation (autonomous mode): `event_status` is the key's mode — events
create as `published` (a key with `event.publish`, the default) or
`pending` (an `api_ingest` key); `entity_status` does the same for entities
(None = no `status` field, as before the TownLoops entity gate). An event
list returns every status unless `status=` filters it, like the server.
`approve` / `reject` / `merge` / `delete` are the moderator's side; a merged
or deleted record GETs as a 404 and no longer lists.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone

from engine.series import series_key
from engine.townloops_api import TownloopsError


EVENT_PATCH_FIELDS = {
    "title", "description", "start_at", "end_at", "venue_name", "venue_id",
    "category_slugs", "business_id", "source_url", "is_featured",
    "accessibility_notes", "tags",
}


class FakeAPI:
    town_slug = "slt"

    def __init__(self, events=None, venues=None, ignore=(), create_drops=(),
                 entities=None, categories=None, series=None,
                 event_status="published", entity_status=None):
        self.events = {e["slug"]: {"series_slug": None, "series_id": None, **copy.deepcopy(e)}
                       for e in (events or [])}
        self.venues = {v["slug"]: dict(v) for v in (venues or [])}
        self.series = {s["slug"]: {"is_active": True, "cadence_text": None, "town": "slt",
                                   "description": None, **s} for s in (series or [])}
        self.tables = {"events": self.events, "venues": self.venues, "series": self.series}
        for resource, rows in (entities or {}).items():
            self.tables.setdefault(resource, {}).update({r["slug"]: dict(r) for r in rows})
        self.categories = set(categories) if categories is not None else None
        self.ignore = set(ignore)  # fields the "server" drops — forces a verify mismatch
        self.create_drops = set(create_drops)
        self.calls: list[tuple] = []
        self.event_status = event_status
        self.entity_status = entity_status
        self.deleted: dict[tuple[str, str], dict] = {}
        self.merged_into: dict[tuple[str, str], str] = {}
        self.created: list[tuple[str, str]] = []   # (resource, slug) of each successful create

    def verify_town(self, *a, **k):
        return {"town_slug": "slt"}

    def get(self, resource, slug):
        self.calls.append(("GET", resource, slug))
        table = self.tables.setdefault(resource, {})
        # Town-scoped like the server: another town's venue is a 404 here.
        if slug not in table or table[slug].get("town", "slt") != "slt":
            self._not_found(resource, slug)
        return copy.deepcopy(table[slug])

    def _not_found(self, resource, slug):
        into = self.merged_into.get((resource, slug))
        raise TownloopsError(404, "not_found", f"{resource} {slug!r} not found",
                             {"merged_into_slug": into} if into else None)

    def list(self, resource, **params):
        self.calls.append(("LIST", resource, dict(params)))
        rows = list(self.tables.setdefault(resource, {}).values())
        if resource == "series":
            rows = sorted((r for r in rows if r["is_active"] and r["town"] == "slt"),
                          key=lambda r: r["title"])
        if params.get("series"):
            s = self.series.get(params["series"])
            rows = [r for r in rows if s and s["town"] == "slt" and r.get("series_slug") == s["slug"]]
        if params.get("status"):
            rows = [r for r in rows if r.get("status", "published") == params["status"]]
        if "ingest_ref" in params:
            rows = [r for r in rows if r.get("ingest_ref") == params["ingest_ref"]]
        rows = sorted(rows, key=lambda r: r["start_at"]) if resource == "events" else rows
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
        _no_series_writes(resource)
        assert "slug" not in payload, f"a {resource} create proposed a slug: {payload['slug']!r}"
        table = self.tables.setdefault(resource, {})
        ref = payload.get("ingest_ref")
        holder = self.ref_holder(resource, ref)
        if holder:
            raise TownloopsError(409, "ingest_ref_taken", f"ingest_ref {ref!r} taken",
                                 {"ingest_ref": ref, "slug": holder})
        slug = self._new_slug(resource, payload.get("title") or payload.get("name"))
        self.created.append((resource, slug))
        if resource != "events":
            row = {**copy.deepcopy(payload), "slug": slug, "id": 1000 + len(table)}
            if self.entity_status:
                row["status"] = self.entity_status
            table[slug] = row
            return copy.deepcopy(row)
        row = {"id": len(self.events) + 1, "slug": slug, "town_slug": "slt",
               "venue_id": None, "venue_slug": None, "category_slugs": [], "tags": [],
               "status": self.event_status, "series_slug": None, "series_id": None,
               "ingest_ref": ref}
        for k in EVENT_PATCH_FIELDS - {"venue_id", "venue_name"}:
            if k in payload and k not in self.create_drops:
                row[k] = payload[k]
        self._bind_venue(row, payload.get("venue_id"), payload.get("venue_name"),
                         payload.get("venue_ingest_ref"))
        row["category_slugs"] = sorted(row["category_slugs"])
        row["tags"] = sorted(_slug(t) for t in row["tags"])
        self._auto_link(row)
        self.events[row["slug"]] = row
        return copy.deepcopy(row)

    def ref_holder(self, resource, ref):
        """Slug of the row holding `ref` in the town (a merged-away row's
        survivor; a deleted row's own slug), else None."""
        if not ref:
            return None
        for slug, r in self.tables.get(resource, {}).items():
            if r.get("ingest_ref") == ref:
                return slug
        for (res, slug), r in self.deleted.items():
            if res == resource and r.get("ingest_ref") == ref:
                return self.merged_into.get((res, slug)) or slug
        return None

    def _new_slug(self, resource, title):
        base = _slug(title or "") or resource
        taken = set(self.tables.get(resource, {})) | {s for r, s in self.deleted if r == resource}
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
        return slug

    def rename(self, resource, old, new):
        """A curator renames a live row: the old slug 404s, naming no survivor."""
        table = self.tables[resource]
        row = table.pop(old)
        row["slug"] = new
        table[new] = row
        if resource == "venues":
            for e in self.events.values():
                if e.get("venue_slug") == old:
                    e["venue_slug"] = new

    def _set_ref(self, resource, slug, row, ref):
        """The set-once PATCH of `ingest_ref`."""
        if ref is None:
            return
        if row.get("ingest_ref") and row["ingest_ref"] != ref:
            raise TownloopsError(422, "validation_error", "ingest_ref is set once")
        holder = self.ref_holder(resource, ref)
        if holder and holder != slug:
            raise TownloopsError(409, "ingest_ref_taken", f"ingest_ref {ref!r} taken",
                                 {"ingest_ref": ref, "slug": holder})
        row["ingest_ref"] = ref

    def _bind_venue(self, row, venue_id, venue_name, venue_ref=None):
        if venue_id is None and not venue_name and not venue_ref:
            return
        town = row.get("town_slug", "slt")
        v = None
        if venue_id is not None:
            v = next((v for v in self.venues.values()
                      if v["id"] == venue_id and v.get("town", "slt") == town), None)
        elif venue_ref and any(x.get("ingest_ref") == venue_ref for x in self.venues.values()):
            v = next(x for x in self.venues.values() if x.get("ingest_ref") == venue_ref)
        elif not venue_name:
            pass
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

    def _auto_link(self, row):
        key = series_key(row.get("title"))
        s = next((s for s in self.series.values()
                  if key and s["is_active"] and s["town"] == "slt" and series_key(s["title"]) == key),
                 None)
        if s:
            row["series_slug"], row["series_id"] = s["slug"], s["id"]

    def link(self, slug, series_slug):
        """A curator (or the backfill) puts a live event in a Series."""
        s = self.series[series_slug] if series_slug else None
        self.events[slug]["series_slug"] = s and s["slug"]
        self.events[slug]["series_id"] = s and s["id"]

    def update(self, resource, slug, payload):
        self.calls.append(("PATCH", resource, slug, copy.deepcopy(payload)))
        _no_series_writes(resource)
        if resource != "events":
            return self._update_entity(resource, slug, payload)
        if slug not in self.events:
            self._not_found(resource, slug)
        row = self.events[slug]
        self._set_ref(resource, slug, row, payload.get("ingest_ref"))
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
            self._not_found(resource, slug)
        row = table[slug]
        self._set_ref(resource, slug, row, payload.get("ingest_ref"))
        for k, v in payload.items():
            if k == "ingest_ref" or v is None or k in self.ignore or (k == "name" and v == ""):
                continue
            if k == "category_slug" and self.categories is not None and v not in self.categories:
                v = None
            row[k] = v
        return copy.deepcopy(row)

    # --- the moderator's side ------------------------------------------------

    def approve(self, resource, slug):
        self.tables[resource][slug]["status"] = "published"

    def reject(self, resource, slug):
        self.tables[resource][slug]["status"] = "rejected"

    def merge(self, resource, slug, into, fields=("start_at", "end_at", "venue_id", "venue_slug")):
        """Apply `fields` of pending `slug` onto published `into`, then
        soft-delete `slug` (townloops `merge_event`). A merged entity's 404
        names the survivor (`merged_into_slug`, townloops ADR 0009); an
        event's doesn't. GET and PATCH 404 alike."""
        source = self.tables[resource][slug]
        for f in fields:
            if f in source:
                self.tables[resource][into][f] = copy.deepcopy(source[f])
        self.delete(resource, slug)
        if resource != "events":
            self.merged_into[(resource, slug)] = into

    def delete(self, resource, slug):
        self.deleted[(resource, slug)] = self.tables[resource].pop(slug)

    def patches(self):
        """Content PATCH bodies — an Ingest Ref adoption (`ref_patches`) aside."""
        return [c[3] for c in self.calls if c[0] == "PATCH" and set(c[3]) != {"ingest_ref"}]

    def ref_patches(self):
        """(resource, slug, ref) of each set-once ref PATCH (adoption)."""
        return [(c[1], c[2], c[3]["ingest_ref"]) for c in self.calls
                if c[0] == "PATCH" and set(c[3]) == {"ingest_ref"}]

    def created_slug(self, resource="events", i=-1):
        """The slug TownLoops derived for the `i`th create of `resource`."""
        return [s for r, s in self.created if r == resource][i]

    def posted_slug(self, resource, payload):
        """The slug of the row a create `payload` made (found by its ingest_ref)."""
        return self.ref_holder(resource, payload["ingest_ref"])

    def ref_of(self, resource, slug):
        return self.tables[resource][slug].get("ingest_ref")

    def creates(self):
        return [c[2] for c in self.calls if c[0] == "POST"]


def _no_series_writes(resource):
    if resource == "series":
        raise TownloopsError(405, "method_not_allowed", "series are read-only")


def _instant(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
