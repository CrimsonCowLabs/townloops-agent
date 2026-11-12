"""The Ingest Ref — the agent's own stable ID for a row it ships, sent on
every create so TownLoops can tell it whether that row already exists
(townloops ADR 0011, issues #28/#29; this repo's #63).

A ref is `hermes-<town>-<uid16>` — the string the agent used to propose as a
slug. It is unique per town per kind on TownLoops and settable once, never
re-pointed. The agent never sends a `slug` on a create: TownLoops derives the
public URL, the ref is the identity.

- A create carrying a ref TownLoops already holds is `409 ingest_ref_taken`,
  whose `details.slug` names the existing row (the survivor when it was
  merged away): bind to it, no second request (`taken_slug`).
- Lookups go by ref first (`find_by_ref`, one `?ingest_ref=` list read); the
  old slug / name + date matches stay only as the fallback for rows shipped
  before refs, and such a row is given its ref through the set-once PATCH as
  soon as it is found — always through `adopt_and_remember` (adopt, remember
  the local column, one `adopt-ref` activity; a final outcome is stamped on
  the local row and never tried again).
- Not every resource takes a ref (#64): `supported(resource)` reads the
  OpenAPI snapshot (`state/openapi.json`) — the list endpoint must declare the
  `ingest_ref` query param and the create payload the property; no snapshot,
  events only. Where unsupported there is no ref lookup, no ref on a create
  (`with_ref`) and no adoption. A lookup accepts only a row that carries the
  ref: a server ignoring the filter (SLT's entity lists answered unrelated
  rows) is `ignored`, never a match — and reported (`ref_filter_ignored`).
- `own_ref` is the ref a local row's live row carries or should: the stored
  one, else ours — none for a live mirror (someone else's row).
- `read_live_event` GETs a live event of ours by slug and, renamed on
  TownLoops (the slug 404s, naming no survivor), finds it by ref and
  re-points the local rows bound to the old slug.

A change or cancellation submission (autonomous mode: a pending event for the
TownLoops merge flow) is a separate row, so it carries its own ref, never the
event's: one per change (`change_ref_for`, keyed by the submitted values, so a
later, different change is a new row), one per event for the cancellation
(`cancel_ref_for`). A merge moves such a ref onto a ref-less survivor (ADR
0011 §Merge), so before submitting one for a live row of ours the agent
adopts the event's own ref on it first (`adopt_before_submission`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from engine.derive_helpers import iso_utc_now, parse_extra_json, record_activity


TAKEN = "ingest_ref_taken"


def _town() -> str:
    return os.environ.get("TOWNLOOPS_TOWN_SLUG", "unknown")


def ref_for(uid: str) -> str:
    """The ref of the event or entity with local uid `uid`."""
    return f"hermes-{_town()}-{uid[:16]}"


def change_ref_for(uid: str, fields: dict) -> str:
    """The ref of the pending change submission carrying `fields` (the
    values submitted) for event `uid`: per change — the identical change
    retried keeps it (and so 409s and binds), a different one gets a new one.
    ≤160 chars (the column's bound)."""
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()
    return f"hermes-{_town()[:120]}-change-{uid[:16]}-{digest[:8]}"


def cancel_ref_for(uid: str) -> str:
    """The ref of event `uid`'s pending cancellation submission (one per
    event)."""
    return f"hermes-{_town()}-cancel-{uid[:16]}"


def taken_slug(exc: Exception) -> Optional[str]:
    """The existing row's slug when `exc` is a `409 ingest_ref_taken`, else
    None (any other error, or a 409 that names no slug)."""
    if getattr(exc, "status", None) != 409:
        return None
    return _named_slug(getattr(exc, "code", None), getattr(exc, "details", None))


def taken_slug_in(error) -> Optional[str]:
    """`taken_slug` for an `error` object as a dict — an `/entries/bulk`
    per-item failure (`{"code", "message", "details"}`) or a raw response's
    `error` — for callers that read responses without the client."""
    if not isinstance(error, dict):
        return None
    return _named_slug(error.get("code"), error.get("details"))


def _named_slug(code, details) -> Optional[str]:
    if code != TAKEN:
        return None
    slug = details.get("slug") if isinstance(details, dict) else None
    return slug if isinstance(slug, str) and slug else None


# --------------------------------------------------------------------------
# Which resources take a ref — from the OpenAPI snapshot
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Capabilities:
    resources: frozenset      # list filter + create property both declared
    venue_ref: bool           # an event create takes `venue_ingest_ref`
    source: str               # the snapshot read, or why none was


# No (readable) snapshot: conservative — the events API is the one known to
# take refs; a venue ref on an event names nothing then.
DEFAULT = Capabilities(frozenset({"events"}), False, "no snapshot: events only")
_cache: dict = {}


def snapshot_path() -> Path:
    """`$TOWNLOOPS_OPENAPI_SNAPSHOT`, else `$HERMES_HOME/state/openapi.json`
    (the townloops-api skill's daily snapshot)."""
    env = os.environ.get("TOWNLOOPS_OPENAPI_SNAPSHOT")
    return Path(env) if env else Path(
        os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / "state" / "openapi.json"


def capabilities() -> Capabilities:
    """What the live API declares, per the snapshot. Re-read whenever the
    snapshot file changes (path, mtime, size), else cached."""
    path = snapshot_path()
    try:
        st = path.stat()
    except OSError:
        return DEFAULT
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _cache.get("key") != key:
        try:
            cap = parse_capabilities(json.loads(path.read_text()), str(path))
        except (OSError, ValueError, AttributeError, TypeError):
            cap = Capabilities(DEFAULT.resources, False, f"{path} unreadable: events only")
        _cache.update(key=key, cap=cap)
    return _cache["cap"]


def parse_capabilities(spec: dict, source: str = "spec") -> Capabilities:
    """`Capabilities` of an OpenAPI document: each `/towns/{town}/<resource>`
    whose GET declares the `ingest_ref` query param and whose POST body
    declares the `ingest_ref` property."""
    resources, venue_ref = set(), False
    for path, ops in (spec.get("paths") or {}).items():
        m = re.search(r"/towns/\{[^}]+\}/([a-z_]+)$", path)
        if not m or not isinstance(ops, dict):
            continue
        get, post = ops.get("get") or {}, ops.get("post") or {}
        params = [_deref(spec, p) for p in
                  (ops.get("parameters") or []) + (get.get("parameters") or [])]
        filters = any(p.get("name") == "ingest_ref" and p.get("in") == "query" for p in params)
        props: set = set()
        for media in (_deref(spec, post.get("requestBody") or {}).get("content") or {}).values():
            props |= _properties(spec, (media or {}).get("schema") or {})
        if filters and "ingest_ref" in props:
            resources.add(m.group(1))
        if m.group(1) == "events" and "venue_ingest_ref" in props:
            venue_ref = True
    return Capabilities(frozenset(resources), venue_ref and "venues" in resources, source)


def _deref(spec: dict, obj, depth: int = 0) -> dict:
    ref = obj.get("$ref") if isinstance(obj, dict) else None
    if not ref or depth > 8 or not ref.startswith("#/"):
        return obj if isinstance(obj, dict) else {}
    node = spec
    for part in ref[2:].split("/"):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return _deref(spec, node, depth + 1)


def _properties(spec: dict, schema, depth: int = 0) -> set:
    schema = _deref(spec, schema)
    out = set(schema.get("properties") or {})
    if depth < 8:
        for key in ("allOf", "anyOf", "oneOf"):
            for sub in schema.get(key) or []:
                out |= _properties(spec, sub, depth + 1)
    return out


def supported(resource: str) -> bool:
    """`resource` (or a kind) takes refs: lookups by ref, refs on creates,
    set-once adoption. See `capabilities`."""
    return RESOURCES.get(resource, resource) in capabilities().resources


def accepts_venue_ref() -> bool:
    """An event create may carry `venue_ingest_ref`."""
    return capabilities().venue_ref


def with_ref(resource: str, payload: dict, ref: Optional[str]) -> bool:
    """Put `ref` on create `payload` when `resource` takes refs (else make
    sure none rides along). True when sent — only then may the created row be
    remembered as carrying it."""
    if ref and supported(resource):
        payload["ingest_ref"] = ref
        return True
    payload.pop("ingest_ref", None)
    return False


# --------------------------------------------------------------------------
# Lookup by ref
# --------------------------------------------------------------------------

@dataclass
class RefLookup:
    row: Optional[dict] = None
    status: str = "none"      # found | none | unsupported | ignored


class RefNotUnique(RuntimeError):
    """Two live rows carry one ref — never pick one."""


def lookup_by_ref(client, resource: str, ref: Optional[str]) -> RefLookup:
    """The live row of `resource` carrying `ref`. Only a returned row whose
    own `ingest_ref` equals `ref` is a match: rows that don't carry it mean
    the server ignored the filter — `ignored`, no match, reported
    (`_report_ignored`). A resource the snapshot says takes no refs is
    `unsupported`, with no request. The list read excludes deleted events, so
    `none` is not a promise a create will succeed (that answers 409,
    `taken_slug`). API errors raise — the caller must not act on an
    unanswered lookup."""
    if not ref:
        return RefLookup()
    if not supported(resource):
        return RefLookup(status="unsupported")
    rows = [r for r in (client.list(resource, ingest_ref=ref, limit=2) or [])
            if isinstance(r, dict)]
    carrying = [r for r in rows if r.get("ingest_ref") == ref]
    if len(carrying) > 1:
        raise RefNotUnique(f"{resource}: {len(carrying)} live rows carry {ref}")
    if carrying:
        return RefLookup(carrying[0], "found")
    if rows:
        _report_ignored(client, resource, ref, rows)
        return RefLookup(status="ignored")
    return RefLookup()


def find_by_ref(client, resource: str, ref: Optional[str]) -> Optional[dict]:
    """`lookup_by_ref`'s row: the live row carrying `ref`, else None — the
    caller falls back to its slug / name matches. API errors raise."""
    return lookup_by_ref(client, resource, ref).row


def _report_ignored(client, resource: str, ref: str, rows: list) -> None:
    """A resource that declares the filter answered rows not carrying the
    ref: stderr, and the client's `ref_filter_ignored` hook when it has one
    (autonomy.GatedClient opens the circuit)."""
    evidence = {"resource": resource, "ingest_ref": ref,
                "returned": [{"slug": r.get("slug"), "ingest_ref": r.get("ingest_ref")}
                             for r in rows[:2]],
                "snapshot": capabilities().source}
    print(f"[warn] {resource}?ingest_ref={ref} answered rows not carrying it "
          f"({', '.join(str(r.get('slug')) for r in rows[:2])}) — filter ignored, no match",
          file=sys.stderr)
    try:
        hook = getattr(client, "ref_filter_ignored", None)
    except Exception:  # noqa: BLE001 — a client without the hook
        hook = None
    if callable(hook):
        hook(resource, evidence)


# Per kind, the TownLoops resource — which is also the local table. The one
# map: engine/entity_sync.RESOURCES is this minus "event".
RESOURCES = {"event": "events", "venue": "venues", "business": "businesses",
             "community": "communities", "performer": "performers", "creator": "creators"}


def uid_column(kind: str) -> str:
    return "event_uid" if kind == "event" else "entity_uid"


def own_ref(kind: str, row) -> Optional[str]:
    """The ref local `row`'s live row carries or should: the stored
    `ingest_ref`, else ours (`ref_for`) — None for a live mirror
    (`source_kind='live'`, a row someone else shipped)."""
    if row["ingest_ref"]:
        return row["ingest_ref"]
    return None if row["source_kind"] == "live" else ref_for(row[uid_column(kind)])


def remember(conn, kind: str, uid: str, ref: str) -> None:
    """Record that the live row of local `kind` `uid` carries `ref`
    (the local `ingest_ref` column, migration 029). No commit."""
    conn.execute(f"UPDATE {RESOURCES[kind]} SET ingest_ref = ? WHERE {uid_column(kind)} = ?",
                 (ref, uid))


# An adoption's outcome is stamped on the local row (`extra_json.ref_adoption`
# = {status, at, detail}); a FINAL one is never tried again — not by the
# sweep (engine/ref_adoption), not by a ship.
MARK = "ref_adoption"
FINAL = ("other-ref", "taken")


def final(row) -> Optional[str]:
    """The final adoption outcome stamped on local `row`, else None."""
    status = (parse_extra_json(row["extra_json"]).get(MARK) or {}).get("status")
    return status if status in FINAL else None


def mark(conn, kind: str, uid: str, status: str, detail: str = "") -> None:
    """Stamp an adoption outcome on the local row. No commit."""
    table, col = RESOURCES[kind], uid_column(kind)
    r = conn.execute(f"SELECT extra_json FROM {table} WHERE {col} = ?", (uid,)).fetchone()
    extra = parse_extra_json(r[0] if r else None)
    extra[MARK] = {"status": status, "at": iso_utc_now(), "detail": detail[:500]}
    conn.execute(f"UPDATE {table} SET extra_json = ? WHERE {col} = ?", (json.dumps(extra), uid))


def clear_mark(conn, kind: str, uid: str) -> None:
    """Drop the adoption stamp, if any. No commit."""
    table, col = RESOURCES[kind], uid_column(kind)
    r = conn.execute(f"SELECT extra_json FROM {table} WHERE {col} = ?", (uid,)).fetchone()
    extra = parse_extra_json(r[0] if r else None)
    if extra.pop(MARK, None) is not None:
        conn.execute(f"UPDATE {table} SET extra_json = ? WHERE {col} = ?",
                     (json.dumps(extra) if extra else None, uid))


@dataclass
class Adoption:
    status: str            # adopted | already | other-ref | taken | error | unsupported
    detail: str = ""


def adopt(client, resource: str, live: dict, ref: str) -> Adoption:
    """Give a live row we shipped before refs existed our ref, through the
    set-once PATCH. A row already carrying it is `already`; one carrying a
    different ref is `other-ref` (never re-pointed — TownLoops refuses with a
    422); a ref another row holds is `taken` (a duplicate, for the operator)
    — unless the 409 names this very row: a row of ours merged into it holds
    the ref, which already resolves here (`already`). Never raises.
    Callers go through `adopt_and_remember`."""
    current = live.get("ingest_ref")
    if current == ref:
        return Adoption("already")
    if current:
        return Adoption("other-ref", f"{live.get('slug')} carries {current}")
    try:
        client.update(resource, live["slug"], {"ingest_ref": ref})
    except Exception as e:  # noqa: BLE001 — reported, retried by the next sweep
        other = taken_slug(e)
        if other and other == live.get("slug"):
            return Adoption("already", f"{ref} resolves to {other} (merged)")
        if other:
            return Adoption("taken", f"{ref} already identifies {other}")
        if getattr(e, "status", None) == 422:
            return Adoption("other-ref", str(e)[:200])
        return Adoption("error", str(e)[:200])
    return Adoption("adopted")


def adopt_and_remember(client, conn, kind: str, uid: str, live: dict, *, worker: str,
                       ref: Optional[str] = None) -> Adoption:
    """The one adoption sequence: give `live` (the live row local `kind`
    `uid` is bound to) our ref (`adopt`; default `ref_for(uid)`); adopted /
    already → remembered locally (`remember`), any stamp cleared; otherwise
    the outcome is stamped (`mark` — `other-ref` / `taken` are FINAL). Every
    outcome but `already` is one `adopt-ref` activity of `worker`, the status
    in its detail; `taken` (a duplicate on TownLoops) and `error` are
    failures. A kind that takes no refs (`supported`) is `unsupported`: no
    PATCH, nothing stamped or recorded — not final, so a later snapshot that
    declares refs adopts it. No commit; never raises on an API error."""
    ref = ref or ref_for(uid)
    if not supported(kind):
        return Adoption("unsupported", f"{RESOURCES[kind]} take no ingest_ref "
                                       f"({capabilities().source})")
    result = adopt(client, RESOURCES[kind], live, ref)
    if result.status in ("adopted", "already"):
        remember(conn, kind, uid, ref)
        clear_mark(conn, kind, uid)
    else:
        mark(conn, kind, uid, result.status, result.detail)
    if result.status != "already":
        failed = result.status in ("taken", "error")
        error = None
        if failed:
            error = f"live {kind} {live.get('slug')}: {result.detail or result.status}"
            if result.status == "taken":
                error += " — a duplicate on TownLoops"
        record_activity(conn, worker, "adopt-ref", f"{kind}:{uid}",
                        detail=f"{live.get('slug')} ← {ref}: {result.status} {result.detail}".strip(),
                        ok=not failed, error=error)
    return result


def wants_adoption(kind: str, row) -> bool:
    """Local `row` is bound to a live row of ours that may not carry our ref
    yet: no ref remembered, not a live mirror, no final adoption outcome."""
    return not row["ingest_ref"] and row["source_kind"] != "live" and not final(row)


def adopt_before_submission(client, conn, event: dict, live: dict, *, worker: str) -> None:
    """Before a change or cancellation submission targeting `live`: when the
    local `event` is bound to it (its `townloops_slug`) and it is ours, give
    it the event's own ref first. Otherwise the moderator's merge moves the
    submission's ref onto the ref-less live row (ADR 0011 §Merge), and the
    event's own ref could never be set there (`other-ref` forever)."""
    if event.get("townloops_slug") == live.get("slug") and wants_adoption("event", event):
        adopt_and_remember(client, conn, "event", event["event_uid"], live, worker=worker)


def read_live_event(client, conn, slug: str, *, worker: str) -> dict:
    """The live event at `slug`. Renamed on TownLoops (the GET 404s, naming
    no survivor): found by the ref of the local event of ours bound to `slug`
    (`live_lookup.get_live_event`), and every local event bound to `slug` is
    re-pointed to the row's current slug — the returned row's `slug`. A 404
    no ref answers, and other API errors, raise."""
    from engine.live_lookup import get_live_event
    owner = conn.execute(
        "SELECT event_uid, ingest_ref, source_kind FROM events WHERE townloops_slug = ? "
        "AND COALESCE(source_kind, '') != 'live' "
        "AND COALESCE(status, '') NOT IN ('duplicate', 'superseded') "
        "ORDER BY ingest_ref IS NULL, pushed_at LIMIT 1", (slug,)).fetchone()
    live = get_live_event(client, slug, own_ref("event", owner) if owner else None)
    current = live.get("slug")
    if current and current != slug:
        conn.execute("UPDATE events SET townloops_slug = ? WHERE townloops_slug = ?",
                     (current, slug))
        record_activity(conn, worker, "repointed", f"event:{owner['event_uid']}",
                        detail=f"{slug} now live as {current} (renamed; found by ingest_ref)")
    return live
