"""Venue derivation matches venues already live on TownLoops before creating a
local shell, so ship doesn't POST a duplicate (ticket 05)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from engine.derive_helpers import load_live_venues, match_live_venue
from engine.event_extract import event_uid, upsert_event


GUNBARREL = {"id": 69, "slug": "gunbarrel-tavern", "name": "Gunbarrel Tavern",
             "address": "1001 Heavenly Village Way", "image_path": "/static/g.png"}
TIPSY = {"id": 99, "slug": "tipsy-putt", "name": "Tipsy Putt"}


class TestMatchLiveVenue:
    def test_exact_name_links_live_venue(self):
        m = match_live_venue("Gunbarrel Tavern.", [TIPSY, GUNBARREL])
        assert m.kind == "match"
        assert m.venue["slug"] == "gunbarrel-tavern"

    def test_richest_live_duplicate_wins_regardless_of_order(self):
        shell = {"id": 120, "slug": "gunbarrel-tavern-2", "name": "Gunbarrel Tavern",
                 "address": None, "image_path": None, "description": None}
        rich = {**GUNBARREL, "description": "Bar and grill in Heavenly Village."}
        for rows in ([shell, rich], [rich, shell]):
            m = match_live_venue("Gunbarrel Tavern", rows)
            assert m.venue["slug"] == "gunbarrel-tavern"

    def test_image_breaks_a_tie_between_otherwise_equal_duplicates(self):
        a = {"id": 1, "slug": "b-side", "name": "The Loft", "address": "1 Main St"}
        b = {"id": 2, "slug": "a-side", "name": "The Loft", "address": "1 Main St",
             "image_path": "/static/loft.png"}
        assert match_live_venue("The Loft", [a, b]).venue["slug"] == "a-side"
        assert match_live_venue("The Loft", [b, a]).venue["slug"] == "a-side"

    def test_fully_equal_duplicates_pick_the_same_row_either_way(self):
        a = {"id": 1, "slug": "loft-a", "name": "The Loft", "address": "1 Main St"}
        b = {"id": 2, "slug": "loft-b", "name": "The Loft", "address": "1 Main St"}
        assert (match_live_venue("The Loft", [a, b]).venue["slug"]
                == match_live_venue("The Loft", [b, a]).venue["slug"])

    def test_close_spelling_is_a_fuzzy_match(self):
        m = match_live_venue("Gunbarel Tavern", [TIPSY, GUNBARREL])
        assert m.kind == "match"
        assert m.venue["slug"] == "gunbarrel-tavern"

    def test_exact_match_wins_over_near_misses(self):
        near = {"id": 5, "slug": "gunbarrel-taverns", "name": "Gunbarrel Taverns",
                "description": "x", "address": "y", "image_path": "z"}
        m = match_live_venue("Gunbarrel Tavern", [near, GUNBARREL])
        assert m.kind == "match" and m.venue["slug"] == "gunbarrel-tavern"

    def test_partial_name_goes_to_review_not_a_link(self):
        m = match_live_venue("Gunbarrel", [GUNBARREL])
        assert m.kind == "review"
        assert [c["slug"] for c in m.candidates] == ["gunbarrel-tavern"]

    def test_short_partial_name_goes_to_review(self):
        cuppa = {"id": 7, "slug": "cuppa-tahoe", "name": "Cuppa Tahoe"}
        m = match_live_venue("Cuppa", [TIPSY, cuppa])
        assert m.kind == "review"
        assert [c["slug"] for c in m.candidates] == ["cuppa-tahoe"]

    def test_two_different_close_venues_go_to_review(self):
        a = {"id": 1, "slug": "lakeview-lodges", "name": "Lakeview Lodges"}
        b = {"id": 2, "slug": "lakeview-lodge-2", "name": "Lakeview Lodge 2"}
        m = match_live_venue("Lakeview Lodge", [a, b])
        assert m.kind == "review"
        assert {c["slug"] for c in m.candidates} == {"lakeview-lodges", "lakeview-lodge-2"}

    def test_no_similar_live_venue_is_no_match(self):
        m = match_live_venue("Pumpkin Patch Farm", [TIPSY, GUNBARREL])
        assert m.kind == "none" and m.venue is None


class TestLiveVenueCache:
    """The orchestrator runs derive-venue once per event; the live venue list
    is fetched once and reused across a drain run."""

    def _fetcher(self, rows):
        calls = []

        def fetch():
            calls.append(1)
            return rows
        return fetch, calls

    def test_second_call_within_ttl_reuses_the_cached_list(self, tmp_path):
        fetch, calls = self._fetcher([GUNBARREL])
        path = tmp_path / "cache" / "live-venues.json"
        assert load_live_venues(fetch, path, ttl_seconds=600, now=1000.0) == [GUNBARREL]
        assert load_live_venues(fetch, path, ttl_seconds=600, now=1500.0) == [GUNBARREL]
        assert len(calls) == 1

    def test_expired_cache_is_refetched(self, tmp_path):
        path = tmp_path / "live-venues.json"
        fetch, _ = self._fetcher([GUNBARREL])
        load_live_venues(fetch, path, ttl_seconds=600, now=1000.0)
        fetch2, calls2 = self._fetcher([GUNBARREL, TIPSY])
        assert load_live_venues(fetch2, path, ttl_seconds=600, now=1601.0) == [GUNBARREL, TIPSY]
        assert len(calls2) == 1

    def test_unreadable_cache_is_refetched(self, tmp_path):
        path = tmp_path / "live-venues.json"
        path.write_text("{not json")
        fetch, calls = self._fetcher([TIPSY])
        assert load_live_venues(fetch, path, ttl_seconds=600, now=1000.0) == [TIPSY]
        assert len(calls) == 1


# --------------------------------------------------------------------------
# event-derive-venue
# --------------------------------------------------------------------------

def _load_derive():
    path = Path(__file__).resolve().parent.parent / "skills/business/event-derive-venue/derive.py"
    spec = importlib.util.spec_from_file_location("event_derive_venue", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def derive(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return _load_derive(), fresh_db


def _approved_event(conn, venue, title="Jazz Night"):
    evt = {"title": title, "date": "2026-10-16T20:00:00", "venue": venue,
           "source_url": "https://seed.example/jazz"}
    upsert_event(conn, "Seed", evt)
    uid = event_uid("Seed", title, evt["date"])
    conn.execute("UPDATE events SET state = 'approved', approval_status = 'approved', "
                 "venue_raw = ? WHERE event_uid = ?", (venue, uid))
    return dict(conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())


def _venue_of(conn, uid):
    row = conn.execute("SELECT v.* FROM venues v JOIN events e ON e.derived_venue_uid = v.entity_uid "
                       "WHERE e.event_uid = ?", (uid,)).fetchone()
    return dict(row) if row else None


class TestDeriveVenue:
    def test_live_venue_is_linked_instead_of_creating_a_shell(self, derive):
        mod, conn = derive
        ev = _approved_event(conn, "Gunbarrel Tavern")
        out = mod.derive_venue(conn, ev, lambda: [TIPSY, GUNBARREL])
        assert out.action == "venue-matched-live"
        v = _venue_of(conn, ev["event_uid"])
        assert v["source_kind"] == "live"
        assert v["townloops_slug"] == "gunbarrel-tavern"
        assert json.loads(v["extra_json"])["live_id"] == 69
        assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 1

    def test_no_live_match_creates_a_shell(self, derive):
        mod, conn = derive
        ev = _approved_event(conn, "Pumpkin Patch Farm")
        out = mod.derive_venue(conn, ev, lambda: [TIPSY, GUNBARREL])
        assert out.action == "venue-created"
        v = _venue_of(conn, ev["event_uid"])
        assert v["source_kind"] == "derived" and v["townloops_slug"] is None

    def test_same_name_live_business_does_not_bind_the_event(self, derive, monkeypatch):
        mod, conn = derive
        live = {"venues": [TIPSY],
                "businesses": [{"id": 3, "slug": "cuppa-tahoe", "name": "Cuppa Tahoe",
                                "address": "2054 Lake Tahoe Blvd"}]}

        class Client:
            def verify_town(self):
                pass

            def paginate(self, resource, **_):
                return iter(live[resource])
        monkeypatch.setattr("engine.townloops_api.Client", Client)
        monkeypatch.setenv("TOWNLOOPS_API_KEY", "k")
        ev = _approved_event(conn, "Cuppa Tahoe")
        out = mod.derive_venue(conn, ev, mod.fetch_live_venues)
        assert out.action == "venue-created"
        v = _venue_of(conn, ev["event_uid"])
        assert v["source_kind"] == "derived" and v["townloops_slug"] is None

    def test_live_venue_beats_an_unshipped_local_shell(self, derive):
        mod, conn = derive
        first = _approved_event(conn, "Gunbarrel Tavern", title="Trivia")
        mod.derive_venue(conn, first, lambda: [])          # shell, live list empty
        ev = _approved_event(conn, "Gunbarrel Tavern")
        out = mod.derive_venue(conn, ev, lambda: [GUNBARREL])
        assert out.action == "venue-matched-live"
        assert _venue_of(conn, ev["event_uid"])["townloops_slug"] == "gunbarrel-tavern"

    def test_local_venue_already_on_townloops_is_used_without_a_live_lookup(self, derive):
        mod, conn = derive
        first = _approved_event(conn, "Gunbarrel Tavern", title="Trivia")
        mod.derive_venue(conn, first, lambda: [GUNBARREL])
        ev = _approved_event(conn, "gunbarrel tavern")

        def no_lookup():
            raise AssertionError("live list fetched")
        out = mod.derive_venue(conn, ev, no_lookup)
        assert out.action == "venue-matched"
        assert out.entity_uid == _venue_of(conn, first["event_uid"])["entity_uid"]
        assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 1

    def test_ambiguous_match_parks_even_when_an_unshipped_shell_exists(self, derive):
        mod, conn = derive
        first = _approved_event(conn, "Gunbarrel", title="Trivia")
        mod.derive_venue(conn, first, lambda: [])          # shell named "Gunbarrel"
        ev = _approved_event(conn, "Gunbarrel")
        out = mod.derive_venue(conn, ev, lambda: [GUNBARREL])
        assert out.action == "venue-review"

    def test_ambiguous_match_parks_the_event_without_creating_a_venue(self, derive):
        mod, conn = derive
        ev = _approved_event(conn, "Gunbarrel")
        out = mod.derive_venue(conn, ev, lambda: [GUNBARREL])
        assert out.action == "venue-review"
        assert "gunbarrel-tavern" in out.detail
        row = conn.execute("SELECT state, derived_venue_uid FROM events WHERE event_uid = ?",
                           (ev["event_uid"],)).fetchone()
        assert row["state"] == "stuck-approved" and row["derived_venue_uid"] is None
        assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 0


class TestDeriveVenueMain:
    def _run(self, derive, monkeypatch, venue, live):
        mod, conn = derive
        ev = _approved_event(conn, venue)
        monkeypatch.setattr(mod, "open_db", lambda: conn)
        monkeypatch.setattr(mod, "parse_event_uid_arg", lambda: ev["event_uid"])
        monkeypatch.setenv("TOWNLOOPS_API_KEY", "k")
        monkeypatch.setattr(mod, "load_live_venues", lambda fetch: live())
        return mod.main(), conn, ev

    def test_ambiguous_match_exits_nonzero_so_the_drainer_does_not_advance(self, derive, monkeypatch):
        rc, conn, ev = self._run(derive, monkeypatch, "Gunbarrel", lambda: [GUNBARREL])
        assert rc == 1
        state = conn.execute("SELECT state FROM events WHERE event_uid = ?",
                             (ev["event_uid"],)).fetchone()[0]
        assert state == "stuck-approved"

    def test_live_lookup_failure_retries_instead_of_creating_a_shell(self, derive, monkeypatch):
        def down():
            raise RuntimeError("503")
        rc, conn, _ = self._run(derive, monkeypatch, "Gunbarrel Tavern", down)
        assert rc == 1
        assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 0

    def test_live_match_exits_zero(self, derive, monkeypatch):
        rc, conn, ev = self._run(derive, monkeypatch, "Gunbarrel Tavern", lambda: [GUNBARREL])
        assert rc == 0
        assert _venue_of(conn, ev["event_uid"])["townloops_slug"] == "gunbarrel-tavern"

    def test_without_an_api_key_no_live_lookup_and_a_shell_is_created(self, derive, monkeypatch):
        def no_lookup(fetch):
            raise AssertionError("live list fetched")
        mod, conn = derive
        ev = _approved_event(conn, "Gunbarrel Tavern")
        monkeypatch.setattr(mod, "open_db", lambda: conn)
        monkeypatch.setattr(mod, "parse_event_uid_arg", lambda: ev["event_uid"])
        monkeypatch.setattr(mod, "load_live_venues", no_lookup)
        monkeypatch.delenv("TOWNLOOPS_API_KEY", raising=False)
        assert mod.main() == 0
        assert _venue_of(conn, ev["event_uid"])["source_kind"] == "derived"
