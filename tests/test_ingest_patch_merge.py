"""event-ingest's ship PATCHes a changed row at its cached slug. A curator
may have merged that record into a published one: the 404 then names the
survivor (`merged_into_slug`, townloops ADR 0009). The row is re-pointed at
the survivor (persisted) and the same PATCH retried there — not reset to a
re-POST that would only hit `409 ingest_ref_taken` and never converge. A 404
without a survivor still resets the row for a re-POST."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from engine.derive_helpers import insert_entity
from engine.event_extract import event_uid, upsert_event


def _load_ingest():
    path = Path(__file__).resolve().parent.parent / "skills/business/event-ingest/ingest.py"
    spec = importlib.util.spec_from_file_location("event_ingest_patch_merge", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ingest():
    return _load_ingest()


@pytest.fixture(autouse=True)
def no_row_carries_the_ref(ingest, monkeypatch):
    """A 404 re-reads the row by ingest_ref (#63); here no live row has it."""
    monkeypatch.setattr(ingest.requests, "get", lambda url, **kw: _Resp(200, {"data": []}))


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    return fresh_db


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.headers = status, body, {}
        self.content = b"x"
        self.text = str(body)

    def json(self):
        return self._body


class FakeServer:
    """PATCH /towns/<town>/<seg>/<slug>: 200 for a live slug (`ids` gives its
    id), else a 404 — naming the survivor when the slug was merged away.
    Like TownLoops, a non-5xx/429 response is cached on (Idempotency-Key,
    body) — the path isn't part of it — and replayed on a repeat. `fail_once`
    slugs answer 500 to their first PATCH."""

    def __init__(self, live=(), merged=None, ids=None, fail_once=()):
        self.live, self.merged, self.calls = set(live), dict(merged or {}), []
        self.ids, self.fail_once, self.cache = dict(ids or {}), set(fail_once), {}

    def patch(self, url, headers=None, json=None, timeout=None):
        import json as _json
        key = (headers["Idempotency-Key"], _json.dumps(json, sort_keys=True))
        if key in self.cache:
            return self.cache[key]
        slug = url.rsplit("/", 1)[1]
        self.calls.append(slug)
        if slug in self.fail_once:
            self.fail_once.discard(slug)
            return _Resp(500, {"error": {"code": "internal_error", "message": "boom"}})
        if slug in self.live:
            resp = _Resp(200, {"data": {"slug": slug, "id": self.ids.get(slug)}})
        else:
            details = {"merged_into_slug": self.merged[slug]} if slug in self.merged else None
            resp = _Resp(404, {"error": {"code": "not_found", "message": "gone",
                                         "details": details}})
        self.cache[key] = resp
        return resp


ENTITY_KINDS = ("venue", "business", "community", "performer", "creator")
KINDS = {"event": "events", "venue": "venues", "business": "businesses",
         "community": "communities", "performer": "performers", "creator": "creators"}


def _shipped(conn, kind, slug="hermes-slt-old"):
    """A row shipped earlier at `slug`, changed since (hash differs)."""
    if kind == "event":
        ev = {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
              "source_url": "https://tahoe.example/jazz"}
        upsert_event(conn, "Seed", ev)
        uid, col = event_uid("Seed", ev["title"], ev["date"]), "event_uid"
    else:
        uid, col = "e-uid", "entity_uid"
        insert_entity(conn, KINDS[kind], uid, {"source": "derived", "source_kind": "derived",
                                               "name": "Riva Grill"})
    conn.execute(f"UPDATE {KINDS[kind]} SET townloops_slug = ?, pushed_at = '2026-10-01', "
                 f"pushed_hash = 'stale' WHERE {col} = ?", (slug, uid))
    return col, uid


def _row(conn, kind, col, uid):
    return conn.execute(f"SELECT * FROM {KINDS[kind]} WHERE {col} = ?", (uid,)).fetchone()


def _patch(ingest, conn, kind, col, uid):
    return ingest._patch_one(conn, kind, _row(conn, kind, col, uid), "https://tl.example",
                             "slt", "k", False)


@pytest.mark.parametrize("kind", ENTITY_KINDS)
def test_merged_row_is_repointed_and_patched_on_the_survivor(ingest, conn, monkeypatch, kind):
    """Only entity 404s name a survivor (townloops ADR 0009)."""
    server = FakeServer(live={"riva-grill"}, merged={"hermes-slt-old": "riva-grill"},
                        ids={"riva-grill": 9})
    monkeypatch.setattr(ingest.requests, "patch", server.patch)
    col, uid = _shipped(conn, kind)

    assert _patch(ingest, conn, kind, col, uid) == (1, 0, [])

    r = _row(conn, kind, col, uid)
    assert server.calls == ["hermes-slt-old", "riva-grill"]
    assert r["townloops_slug"] == "riva-grill"
    assert r["pushed_hash"] == r["content_hash"] and r["pushed_at"]
    # slug and live id move together (event-ship binds venues by the id)
    assert json.loads(r["extra_json"])["live_id"] == 9


def test_stale_live_id_is_dropped_when_the_survivor_patch_fails(ingest, conn, monkeypatch):
    server = FakeServer(live={"riva-grill"}, merged={"hermes-slt-old": "riva-grill"},
                        fail_once={"riva-grill"})
    monkeypatch.setattr(ingest.requests, "patch", server.patch)
    col, uid = _shipped(conn, "venue")
    conn.execute("UPDATE venues SET extra_json = '{\"live_id\": 3, \"note\": \"x\"}'")

    _patch(ingest, conn, "venue", col, uid)

    r = _row(conn, "venue", col, uid)
    assert r["townloops_slug"] == "riva-grill"
    assert json.loads(r["extra_json"]) == {"note": "x"}


def test_next_run_after_a_failed_survivor_patch_lands(ingest, conn, monkeypatch):
    """The server replays by key + body, not path: the survivor's PATCH must
    not reuse the key that got the old slug's 404."""
    server = FakeServer(live={"riva-grill"}, merged={"hermes-slt-old": "riva-grill"},
                        fail_once={"riva-grill"})
    monkeypatch.setattr(ingest.requests, "patch", server.patch)
    col, uid = _shipped(conn, "venue")
    assert _patch(ingest, conn, "venue", col, uid)[:2] == (0, 1)

    assert _patch(ingest, conn, "venue", col, uid) == (1, 0, [])
    r = _row(conn, "venue", col, uid)
    assert r["townloops_slug"] == "riva-grill" and r["pushed_hash"] == r["content_hash"]


def test_event_404_resets_for_a_re_post(ingest, conn, monkeypatch):
    """Event 404s never name a survivor: an event gone live is re-POSTed."""
    server = FakeServer()
    monkeypatch.setattr(ingest.requests, "patch", server.patch)
    col, uid = _shipped(conn, "event")
    _patch(ingest, conn, "event", col, uid)
    assert _row(conn, "event", col, uid)["townloops_slug"] is None


def test_later_changes_patch_the_survivor_directly(ingest, conn, monkeypatch):
    server = FakeServer(live={"riva-grill"}, merged={"hermes-slt-old": "riva-grill"})
    monkeypatch.setattr(ingest.requests, "patch", server.patch)
    col, uid = _shipped(conn, "venue")
    _patch(ingest, conn, "venue", col, uid)
    conn.execute("UPDATE venues SET description = 'Lakeside grill.', content_hash = 'h2' "
                 "WHERE entity_uid = ?", (uid,))
    server.calls.clear()

    assert _patch(ingest, conn, "venue", col, uid) == (1, 0, [])
    assert server.calls == ["riva-grill"]


def test_survivor_is_kept_even_when_its_patch_fails(ingest, conn, monkeypatch):
    """The re-point is persisted before the retry: a transient failure on
    the survivor leaves the row bound there for the next run."""
    server = FakeServer(merged={"hermes-slt-old": "riva-grill"})
    real_patch = server.patch

    def unavailable(url, **kw):
        resp = real_patch(url, **kw)
        return _Resp(500, {}) if url.endswith("/riva-grill") else resp
    monkeypatch.setattr(ingest.requests, "patch", unavailable)
    col, uid = _shipped(conn, "venue")

    shipped, failed, _ = _patch(ingest, conn, "venue", col, uid)

    r = _row(conn, "venue", col, uid)
    assert (shipped, failed) == (0, 1)
    assert r["townloops_slug"] == "riva-grill" and r["pushed_at"]


def test_404_without_a_survivor_resets_for_a_re_post(ingest, conn, monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(ingest.requests, "patch", server.patch)
    col, uid = _shipped(conn, "venue")

    shipped, failed, fails = _patch(ingest, conn, "venue", col, uid)

    r = _row(conn, "venue", col, uid)
    assert (shipped, failed) == (0, 1) and fails[0]["error"]["code"] == "not_found_resetting"
    assert r["townloops_slug"] is None and r["pushed_at"] is None and r["pushed_hash"] is None


def test_merge_loop_is_followed_a_bounded_number_of_times(ingest, conn, monkeypatch):
    from engine.live_lookup import MAX_MERGE_HOPS
    server = FakeServer(merged={"hermes-slt-old": "b", "b": "hermes-slt-old"})
    monkeypatch.setattr(ingest.requests, "patch", server.patch)
    col, uid = _shipped(conn, "venue")

    shipped, failed, _ = _patch(ingest, conn, "venue", col, uid)

    assert (shipped, failed) == (0, 1)
    assert len(server.calls) <= MAX_MERGE_HOPS + 1
    # Merged, so not reset for a re-POST that could only hit ingest_ref_taken.
    assert _row(conn, "venue", col, uid)["townloops_slug"] is not None
