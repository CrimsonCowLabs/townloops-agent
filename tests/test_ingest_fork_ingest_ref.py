"""event-ingest (the legacy fork) ships with the Ingest Ref (#63, townloops
ADR 0011): every create carries `ingest_ref` and never a `slug`; a per-item
`409 ingest_ref_taken` in `/entries/bulk` means "already shipped" and binds
the row to `details.slug` with no second request; `resync` matches live rows
by ref first and by name + date only for rows without one, adopting the ref
on such a row through the set-once PATCH."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from engine import ingest_ref
from engine.derive_helpers import insert_entity
from engine.event_extract import event_uid, upsert_event


def _load_ingest():
    path = Path(__file__).resolve().parent.parent / "skills/business/event-ingest/ingest.py"
    spec = importlib.util.spec_from_file_location("event_ingest_ingest_ref", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ingest():
    return _load_ingest()


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    return fresh_db


ENTITY_KINDS = ("venue", "business", "community", "performer", "creator")
TABLES = {"event": "events", "venue": "venues", "business": "businesses",
          "community": "communities", "performer": "performers", "creator": "creators"}


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.headers = status, body, {}
        self.content = b"x"
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeTownLoops:
    """Just enough of TownLoops for the fork: `/entries/bulk` (per-item
    `ingest_ref_taken` naming the existing row), the paged list read with its
    `?ingest_ref=` filter, and PATCH (an `ingest_ref` is set once — a
    different one is a 422)."""

    SEG = {"events": "event", "venues": "venue", "businesses": "business",
           "communities": "community", "performers": "performer", "creators": "creator"}

    def __init__(self):
        self.rows: dict[str, dict[str, dict]] = {k: {} for k in TABLES}
        self.posted: list[dict] = []
        self.patches: list[tuple[str, str, dict]] = []
        self.gets: list[str] = []
        self.next_id = 1

    def live(self, kind, slug, **fields):
        row = {"slug": slug, "id": self.next_id, "updated_at": "2026-10-01T00:00:00Z", **fields}
        self.next_id += 1
        self.rows[kind][slug] = row
        return row

    def _by_ref(self, kind, ref):
        return next((r for r in self.rows[kind].values() if r.get("ingest_ref") == ref), None)

    def post(self, url, headers=None, json=None, timeout=None):
        assert url.endswith("/entries/bulk")
        out = []
        for entry in json:
            kind, payload = entry["entity_type"], entry["payload"]
            self.posted.append(entry)
            held = self._by_ref(kind, payload.get("ingest_ref"))
            if held:
                out.append({"ok": False, "error": {
                    "code": "ingest_ref_taken", "message": "taken",
                    "details": {"ingest_ref": payload["ingest_ref"], "slug": held["slug"]}}})
                continue
            base = re.sub(r"[^a-z0-9]+", "-", (payload.get("title") or payload["name"]).lower()).strip("-")
            slug, n = base, 2
            while slug in self.rows[kind]:
                slug, n = f"{base}-{n}", n + 1
            name_key = "title" if kind == "event" else "name"
            row = self.live(kind, slug, **{name_key: payload.get(name_key)},
                            ingest_ref=payload.get("ingest_ref"),
                            start_at=payload.get("start_at"))
            out.append({"ok": True, "data": {"slug": slug, "id": row["id"]}})
        return _Resp(207, {"data": out})

    def get(self, url, headers=None, timeout=None, params=None):
        self.gets.append(url)
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        q.update(params or {})
        kind = self.SEG[u.path.rsplit("/", 1)[1]]
        rows = list(self.rows[kind].values())
        if "ingest_ref" in q:
            rows = [r for r in rows if r.get("ingest_ref") == q["ingest_ref"]]
        offset, limit = int(q.get("offset", 0)), int(q.get("limit", 200))
        return _Resp(200, {"data": rows[offset:offset + limit]})

    def patch(self, url, headers=None, json=None, timeout=None):
        parts = urlparse(url).path.rsplit("/", 2)
        kind, slug = self.SEG[parts[1]], parts[2]
        self.patches.append((kind, slug, json))
        row = self.rows[kind].get(slug)
        if row is None:
            return _Resp(404, {"error": {"code": "not_found", "message": "gone"}})
        ref = json.get("ingest_ref")
        if ref is not None:
            if row.get("ingest_ref") and row["ingest_ref"] != ref:
                return _Resp(422, {"error": {"code": "validation_error", "message": "set once"}})
            other = self._by_ref(kind, ref)
            if other and other is not row:
                return _Resp(409, {"error": {"code": "ingest_ref_taken", "message": "taken",
                                             "details": {"ingest_ref": ref, "slug": other["slug"]}}})
            row["ingest_ref"] = ref
        return _Resp(200, {"data": {"slug": slug, "id": row["id"]}})


@pytest.fixture
def server(ingest, monkeypatch):
    s = FakeTownLoops()
    monkeypatch.setattr(ingest.requests, "post", s.post)
    monkeypatch.setattr(ingest.requests, "get", s.get)
    monkeypatch.setattr(ingest.requests, "patch", s.patch)
    return s


def _event(conn, title="Jazz Night", date="2026-10-09T20:00:00", venue=None):
    ev = {"title": title, "date": date, "source_url": "https://tahoe.example/jazz"}
    upsert_event(conn, "Seed", ev)
    uid = event_uid("Seed", title, date)
    if venue:
        conn.execute("UPDATE events SET venue_name = ? WHERE event_uid = ?", (venue, uid))
    return uid


def _entity(conn, kind, name="Riva Grill", uid=None):
    uid = uid or f"{kind}-uid-0000000000000000"
    insert_entity(conn, TABLES[kind], uid, {"source": "derived", "source_kind": "derived",
                                            "name": name})
    return uid


def _rows(conn, kind):
    return list(conn.execute(f"SELECT * FROM {TABLES[kind]}"))


def _bulk(ingest, conn, kind):
    rows = ingest._select_unpushed_by_kind(conn, kind)
    return ingest._bulk_post(conn, kind, rows, "https://tl.example/towns/test-town/entries/bulk",
                             "k", 100, False)


def _ship(ingest, conn, monkeypatch):
    monkeypatch.setattr(ingest, "connect", lambda: conn)
    monkeypatch.setattr(ingest, "apply_migrations", lambda c: None)
    args = argparse.Namespace(dry_run=False, town=None, batch_size=100, only_kind=None)
    return ingest._cmd_ship(args)


# --- creates carry the ref, never a slug --------------------------------------

@pytest.mark.parametrize("kind", ["event", *ENTITY_KINDS])
def test_bulk_create_carries_the_ref_and_no_slug(ingest, conn, server, kind):
    uid = _event(conn) if kind == "event" else _entity(conn, kind)

    assert _bulk(ingest, conn, kind)[:2] == (1, 0)

    (entry,) = server.posted
    assert "slug" not in entry["payload"]
    assert entry["payload"]["ingest_ref"] == ingest_ref.ref_for(uid)


@pytest.mark.parametrize("kind", ["event", *ENTITY_KINDS])
def test_dry_run_entry_has_no_slug(ingest, conn, kind):
    _event(conn) if kind == "event" else _entity(conn, kind)
    (row,) = ingest._select_unpushed_by_kind(conn, kind)
    entry = ingest._row_to_bulk_entry(row, kind, conn)
    assert "slug" not in entry["payload"] and entry["payload"]["ingest_ref"].startswith("hermes-")


@pytest.mark.parametrize("kind", ["event", *ENTITY_KINDS])
def test_a_successful_create_remembers_the_ref_and_slug(ingest, conn, server, kind):
    uid = _event(conn) if kind == "event" else _entity(conn, kind)
    _bulk(ingest, conn, kind)
    (row,) = _rows(conn, kind)
    assert row["ingest_ref"] == ingest_ref.ref_for(uid)
    assert row["townloops_slug"] and row["pushed_hash"] == row["content_hash"]


def test_event_names_its_venue_by_ref_when_the_venue_is_known(ingest, conn, server):
    venue_uid = ingest._entity_uid("venue", "Riva Grill")
    _entity(conn, "venue", name="Riva Grill", uid=venue_uid)
    _event(conn, venue="Riva Grill")
    _event(conn, title="Open Mic", venue="Somewhere Unknown")

    _bulk(ingest, conn, "event")

    by_title = {e["payload"]["title"]: e["payload"] for e in server.posted}
    assert by_title["Jazz Night"]["venue_ingest_ref"] == ingest_ref.ref_for(venue_uid)
    assert by_title["Jazz Night"]["venue_name"] == "Riva Grill"
    assert "venue_ingest_ref" not in by_title["Open Mic"]
    assert by_title["Open Mic"]["venue_name"] == "Somewhere Unknown"


# --- 409 ingest_ref_taken = already shipped -------------------------------------

def test_bulk_mixing_ok_and_taken_binds_each_conflict_and_ships_the_rest(ingest, conn, server):
    a = _event(conn, title="Jazz Night")
    b = _event(conn, title="Open Mic")
    c = _event(conn, title="Trivia")
    server.live("event", "jazz-night-renamed", title="Jazz Night", ingest_ref=ingest_ref.ref_for(a))
    server.live("event", "trivia", title="Trivia", ingest_ref=ingest_ref.ref_for(c))

    shipped, failed, failures = _bulk(ingest, conn, "event")

    assert (shipped, failed, failures) == (3, 0, [])
    assert len(server.posted) == 3  # one request, no second one for the conflicts
    assert server.gets == [] and server.patches == []
    rows = {r["event_uid"]: r for r in _rows(conn, "event")}
    assert rows[a]["townloops_slug"] == "jazz-night-renamed"
    assert rows[c]["townloops_slug"] == "trivia"
    assert rows[b]["townloops_slug"] == "open-mic"
    for uid in (a, b, c):
        assert rows[uid]["ingest_ref"] == ingest_ref.ref_for(uid)
        assert rows[uid]["pushed_hash"] == rows[uid]["content_hash"]
    assert len(server.rows["event"]) == 3


def test_a_taken_conflict_without_a_slug_is_a_failure(ingest, conn, monkeypatch):
    _event(conn)

    def post(url, **kw):
        return _Resp(207, {"data": [{"ok": False, "error": {
            "code": "ingest_ref_taken", "message": "taken", "details": None}}]})
    monkeypatch.setattr(ingest.requests, "post", post)

    shipped, failed, _ = _bulk(ingest, conn, "event")
    (row,) = _rows(conn, "event")
    assert (shipped, failed) == (0, 1) and row["pushed_at"] is None


def test_a_wiped_local_cache_reship_creates_nothing_new(ingest, conn, server, monkeypatch):
    _event(conn, title="Jazz Night", venue="Riva Grill")
    _entity(conn, "venue", name="Riva Grill", uid=ingest._entity_uid("venue", "Riva Grill"))
    _entity(conn, "performer", name="The Band")
    assert _ship(ingest, conn, monkeypatch) == 0
    before = {k: dict(v) for k, v in server.rows.items()}
    bound = {k: {r["ingest_ref"]: r["townloops_slug"] for r in _rows(conn, k)} for k in TABLES}

    # Wipe every bit of ship state, as a fresh clone would have it.
    for table in TABLES.values():
        conn.execute(f"UPDATE {table} SET pushed_at = NULL, pushed_hash = NULL, "
                     f"townloops_slug = NULL, ingest_ref = NULL")
    server.posted.clear()

    assert _ship(ingest, conn, monkeypatch) == 0

    assert {k: set(v) for k, v in server.rows.items()} == {k: set(v) for k, v in before.items()}
    assert len(server.posted) == 3
    for k in TABLES:
        assert {r["ingest_ref"]: r["townloops_slug"] for r in _rows(conn, k)} == bound[k]


def test_a_reship_after_a_townloops_rename_creates_nothing_and_patches_the_new_slug(
        ingest, conn, server, monkeypatch):
    uid = _event(conn)
    _ship(ingest, conn, monkeypatch)
    live = server.rows["event"].pop("jazz-night")
    live["slug"] = "jazz-night-at-riva"
    server.rows["event"]["jazz-night-at-riva"] = live
    conn.execute("UPDATE events SET description = 'Now with a trio.', content_hash = 'h2' "
                 "WHERE event_uid = ?", (uid,))

    assert _ship(ingest, conn, monkeypatch) == 0

    (row,) = _rows(conn, "event")
    assert row["townloops_slug"] == "jazz-night-at-riva"
    assert row["pushed_hash"] == "h2"
    assert len(server.rows["event"]) == 1
    assert server.patches[-1][1] == "jazz-night-at-riva"


# --- PATCH targets ---------------------------------------------------------------

def test_patch_uses_the_captured_slug_not_the_ref(ingest, conn, server):
    uid = _entity(conn, "venue")
    server.live("venue", "riva-grill", name="Riva Grill")
    conn.execute("UPDATE venues SET townloops_slug = 'riva-grill', pushed_at = 'x', "
                 "pushed_hash = 'stale' WHERE entity_uid = ?", (uid,))
    (row,) = _rows(conn, "venue")

    assert ingest._patch_one(conn, "venue", row, "https://tl.example", "test-town", "k", False)[:2] == (1, 0)
    assert server.patches[0][1] == "riva-grill" and server.gets == []


def test_patch_without_a_captured_slug_finds_the_row_by_ref(ingest, conn, server):
    uid = _entity(conn, "venue")
    server.live("venue", "riva-grill", name="Riva Grill", ingest_ref=ingest_ref.ref_for(uid))
    conn.execute("UPDATE venues SET pushed_at = 'x', pushed_hash = 'stale' WHERE entity_uid = ?", (uid,))
    (row,) = _rows(conn, "venue")

    assert ingest._patch_one(conn, "venue", row, "https://tl.example", "test-town", "k", False)[:2] == (1, 0)
    assert [p[1] for p in server.patches] == ["riva-grill"]
    (row,) = _rows(conn, "venue")
    assert row["townloops_slug"] == "riva-grill" and row["ingest_ref"] == ingest_ref.ref_for(uid)


# --- resync ---------------------------------------------------------------------

def _resync(ingest, conn, kind, dry_run=False, delete_orphans=False):
    return ingest._resync_kind(conn, kind, "https://tl.example", "test-town", "k",
                               dry_run, delete_orphans=delete_orphans)


def test_resync_binds_by_ref_first(ingest, conn, server):
    uid = _event(conn, title="Jazz Night")
    # Renamed live (title no longer matches) but carrying our ref.
    server.live("event", "jazz-at-riva", title="Jazz at Riva", start_at="2026-10-10T03:00:00Z",
                ingest_ref=ingest_ref.ref_for(uid))
    # A same-name same-day row with no ref must not win over it.
    server.live("event", "jazz-night", title="Jazz Night", start_at="2026-10-10T03:00:00Z")

    out = _resync(ingest, conn, "event")

    (row,) = _rows(conn, "event")
    assert row["townloops_slug"] == "jazz-at-riva"
    assert row["ingest_ref"] == ingest_ref.ref_for(uid)
    assert out["matched_by_ref"] == 1 and out["matched_by_name"] == 0
    assert server.patches == []


def test_resync_falls_back_to_name_and_date_and_adopts_the_ref(ingest, conn, server):
    uid = _event(conn, title="Jazz Night")
    server.live("event", "jazz-night", title="Jazz Night", start_at="2026-10-10T03:00:00Z")

    out = _resync(ingest, conn, "event")

    (row,) = _rows(conn, "event")
    assert row["townloops_slug"] == "jazz-night"
    assert out["matched_by_name"] == 1 and out["refs_adopted"] == 1
    assert server.patches == [("event", "jazz-night", {"ingest_ref": ingest_ref.ref_for(uid)})]
    assert server.rows["event"]["jazz-night"]["ingest_ref"] == ingest_ref.ref_for(uid)
    assert row["ingest_ref"] == ingest_ref.ref_for(uid)


def test_resync_fallback_skips_live_rows_carrying_another_ref(ingest, conn, server):
    _event(conn, title="Jazz Night")
    server.live("event", "jazz-night", title="Jazz Night", start_at="2026-10-10T03:00:00Z",
                ingest_ref="hermes-test-town-someoneelse0000")

    out = _resync(ingest, conn, "event")

    (row,) = _rows(conn, "event")
    assert row["townloops_slug"] is None and out["unmatched_local"] == 1
    assert server.patches == []


def test_resync_dry_run_adopts_nothing(ingest, conn, server):
    _entity(conn, "venue")
    server.live("venue", "riva-grill", name="Riva Grill")

    out = _resync(ingest, conn, "venue", dry_run=True)

    assert out["matched_by_name"] == 1 and server.patches == []
    (row,) = _rows(conn, "venue")
    assert row["townloops_slug"] is None and row["ingest_ref"] is None


@pytest.mark.parametrize("kind", ENTITY_KINDS)
def test_resync_entities_by_ref(ingest, conn, server, kind):
    uid = _entity(conn, kind)
    server.live(kind, "renamed", name="Something Else", ingest_ref=ingest_ref.ref_for(uid))

    out = _resync(ingest, conn, kind)

    (row,) = _rows(conn, kind)
    assert row["townloops_slug"] == "renamed" and out["matched_by_ref"] == 1


def test_delete_orphans_never_deletes_a_row_carrying_a_ref(ingest, conn, server, monkeypatch):
    deleted = []
    monkeypatch.setattr(ingest, "_delete_townloops_row",
                        lambda base, town, kind, slug, key: deleted.append(slug) or 204)
    uid = _event(conn, title="Jazz Night")
    server.live("event", "jazz-night-2", title="Jazz Night", start_at="2026-10-10T03:00:00Z",
                ingest_ref=ingest_ref.ref_for(uid), updated_at="2026-09-01")
    server.live("event", "jazz-night", title="Jazz Night", start_at="2026-10-10T03:00:00Z")
    server.live("event", "jazz-night-3", title="Jazz Night", start_at="2026-10-10T03:00:00Z",
                ingest_ref="hermes-test-town-someoneelse0000")

    out = _resync(ingest, conn, "event", delete_orphans=True)

    (row,) = _rows(conn, "event")
    assert row["townloops_slug"] == "jazz-night-2"
    assert deleted == ["jazz-night"]
    assert out["true_orphans"] == 1


def test_a_failed_adoption_still_binds_the_slug_and_is_reported(ingest, conn, server, monkeypatch):
    _event(conn, title="Jazz Night")
    server.live("event", "jazz-night", title="Jazz Night", start_at="2026-10-10T03:00:00Z")
    monkeypatch.setattr(ingest.requests, "patch",
                        lambda url, **kw: _Resp(503, {"error": {"code": "unavailable", "message": "x"}}))

    out = _resync(ingest, conn, "event")

    (row,) = _rows(conn, "event")
    assert row["townloops_slug"] == "jazz-night" and row["ingest_ref"] is None
    assert out["refs_adopted"] == 0 and out["ref_adopt_failed"] == 1
    assert out["ref_adopt_failure_sample"][0]["status"] == "error"


def test_taken_slug_in_reads_a_bulk_item_error():
    err = {"code": "ingest_ref_taken", "message": "x",
           "details": {"ingest_ref": "hermes-slt-1", "slug": "jazz-2026-08-01"}}
    assert ingest_ref.taken_slug_in(err) == "jazz-2026-08-01"
    assert ingest_ref.taken_slug_in({"code": "slug_taken", "details": {"slug": "a"}}) is None
    assert ingest_ref.taken_slug_in({"code": "ingest_ref_taken", "details": None}) is None
    assert ingest_ref.taken_slug_in(None) is None
