"""An event's own venue is never its host business or community (issue #4):
the venue link already carries it. A host that is some other venue is a
genuine host — kept, with an informational log."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from engine.derive_helpers import (
    VenueNameMatch, cached_live_venues, mirror_live_entity, other_venue_named, own_venue_named,
)
from engine.event_extract import event_uid, upsert_event


CASCADE = {"id": 41, "slug": "cascade-kitchens", "name": "Cascade Kitchens",
           "address": "3668 Lake Tahoe Blvd"}
TIPSY = {"id": 99, "slug": "tipsy-putt", "name": "Tipsy Putt"}
ALIBI = {"id": 12, "slug": "alibi-ale-works", "name": "Alibi Ale Works"}


def _load_derive():
    path = Path(__file__).resolve().parent.parent / "skills/business/event-derive-hosts/derive.py"
    spec = importlib.util.spec_from_file_location("event_derive_hosts", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def derive(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return _load_derive(), fresh_db


def _event(conn, venue, host, title="Plates at Cascade Kitchens", venue_uid=None):
    """A venue-derived event whose people pass (cached) found `host`."""
    evt = {"title": title, "date": "2026-10-16T18:00:00", "venue": venue,
           "source_url": "https://seed.example/plates"}
    upsert_event(conn, "Seed", evt)
    uid = event_uid("Seed", title, evt["date"])
    conn.execute("UPDATE events SET state = 'venue-derived', approval_status = 'approved', "
                 "venue_raw = ?, derived_venue_uid = ?, extra_json = ? WHERE event_uid = ?",
                 (venue, venue_uid, json.dumps({"people": {"host": host, "people": []}}), uid))
    return uid


def _run(mod, conn, monkeypatch, uid, cached_live=()):
    monkeypatch.setattr(mod, "open_db", lambda: conn)
    monkeypatch.setattr(mod, "parse_event_uid_arg", lambda: uid)
    monkeypatch.setattr(mod, "cached_live_venues", lambda: list(cached_live))
    return mod.main()


def _host(conn, uid):
    return conn.execute("SELECT derived_host_uid, derived_host_kind FROM events WHERE event_uid = ?",
                        (uid,)).fetchone()


def _activity(conn, action):
    return [r["detail"] for r in conn.execute(
        "SELECT detail FROM agent_activity WHERE worker = 'event-derive-hosts' AND action = ?",
        (action,))]


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestOwnVenueIsNeverTheHost:
    def test_cascade_kitchens_organizer_is_its_own_venue(self, derive, monkeypatch):
        mod, conn = derive
        vuid = mirror_live_entity(conn, "venue", CASCADE)
        uid = _event(conn, "Cascade Kitchens", venue_uid=vuid,
                     host={"name": "Cascade Kitchens Food Hall & Bar", "kind": "business"})
        assert _run(mod, conn, monkeypatch, uid, [CASCADE]) == 0
        assert tuple(_host(conn, uid)) == (None, None)
        assert _count(conn, "businesses") == 0
        assert conn.execute("SELECT COUNT(*) FROM linked_events WHERE role = 'host'").fetchone()[0] == 0
        [detail] = _activity(conn, "host-is-venue")
        assert "Cascade Kitchens" in detail

    def test_own_venue_matches_by_the_events_venue_name_alone(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Cascade Kitchens",
                     host={"name": "Cascade Kitchens Food Hall & Bar", "kind": "business"})
        assert _run(mod, conn, monkeypatch, uid) == 0
        assert _host(conn, uid)["derived_host_uid"] is None
        assert _count(conn, "businesses") == 0
        assert _activity(conn, "host-is-venue")

    def test_own_venue_matches_by_the_derived_venue_rows_name(self, derive, monkeypatch):
        mod, conn = derive
        vuid = mirror_live_entity(conn, "venue", CASCADE)
        uid = _event(conn, "the food hall on Lake Tahoe Blvd", venue_uid=vuid,
                     host={"name": "Cascade Kitchens Food Hall & Bar", "kind": "business"})
        assert _run(mod, conn, monkeypatch, uid) == 0
        assert _host(conn, uid)["derived_host_uid"] is None
        assert _count(conn, "businesses") == 0

    def test_preset_host_that_is_the_own_venue_is_dropped(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Cascade Kitchens", host=None)
        conn.execute("INSERT INTO businesses (entity_uid, source, source_kind, name, first_seen_at, "
                     "last_seen_at, content_hash, last_modified_at) VALUES "
                     "('b1', 'x', 'derived', 'Cascade Kitchens Food Hall & Bar', 't', 't', 'h', 't')")
        conn.execute("UPDATE events SET derived_host_uid = 'b1', derived_host_kind = 'business' "
                     "WHERE event_uid = ?", (uid,))
        assert _run(mod, conn, monkeypatch, uid) == 0
        assert tuple(_host(conn, uid)) == (None, None)
        assert conn.execute("SELECT COUNT(*) FROM linked_events WHERE role = 'host'").fetchone()[0] == 0
        assert _activity(conn, "host-is-venue")


class TestNoHostClearsAStaleHost:
    def _stale(self, conn, uid):
        conn.execute("UPDATE events SET derived_host_uid = 'gone', derived_host_kind = 'business' "
                     "WHERE event_uid = ?", (uid,))

    def test_no_host_in_the_text_clears_it(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Lakeview Commons", title="Fireworks", host=None)
        self._stale(conn, uid)
        assert _run(mod, conn, monkeypatch, uid) == 0
        assert tuple(_host(conn, uid)) == (None, None)

    def test_ai_unavailable_on_the_last_attempt_clears_it(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Lakeview Commons", title="Fireworks", host=None)
        conn.execute("UPDATE events SET extra_json = '{}' WHERE event_uid = ?", (uid,))
        self._stale(conn, uid)
        monkeypatch.setattr(mod.event_people, "derive", lambda *a, **k: None)
        assert _run(mod, conn, monkeypatch, uid) == 0   # no queue row = last attempt
        assert tuple(_host(conn, uid)) == (None, None)


class TestGenuineHostsStillDerive:
    def test_club_at_a_venue_is_a_community_host(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Cascade Kitchens", title="Rotary Lunch",
                     host={"name": "Rotary Club of Tahoe", "kind": "community"})
        assert _run(mod, conn, monkeypatch, uid, [CASCADE]) == 0
        assert _host(conn, uid)["derived_host_kind"] == "community"
        assert _count(conn, "communities") == 1
        assert not _activity(conn, "host-matches-other-venue")

    def test_brewery_that_is_not_the_venue_is_a_business_host(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Cascade Kitchens", title="Tap Takeover",
                     host={"name": "Alibi Ale Works", "kind": "business"})
        assert _run(mod, conn, monkeypatch, uid, [TIPSY, CASCADE]) == 0
        assert _host(conn, uid)["derived_host_kind"] == "business"
        assert _count(conn, "businesses") == 1

    def test_brewery_that_is_a_local_venue_hosting_elsewhere_is_kept_and_logged(self, derive, monkeypatch):
        mod, conn = derive
        mirror_live_entity(conn, "venue", ALIBI)
        uid = _event(conn, "Lakeview Commons", title="Beer Garden",
                     host={"name": "Alibi Ale Works", "kind": "business"})
        assert _run(mod, conn, monkeypatch, uid) == 0
        assert _host(conn, uid)["derived_host_kind"] == "business"
        assert _count(conn, "businesses") == 1
        [detail] = _activity(conn, "host-matches-other-venue")
        assert "Alibi Ale Works" in detail and "alibi-ale-works" in detail

    def test_brewery_that_is_a_live_venue_hosting_elsewhere_is_kept_and_logged(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Lakeview Commons", title="Beer Garden",
                     host={"name": "Alibi Ale Works Brewing", "kind": "business"})
        assert _run(mod, conn, monkeypatch, uid, [TIPSY, ALIBI]) == 0
        assert _host(conn, uid)["derived_host_kind"] == "business"
        [detail] = _activity(conn, "host-matches-other-venue")
        assert "alibi-ale-works" in detail

    def test_foundation_named_after_its_venue_is_still_a_host(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Lake Tahoe Community College", title="Scholarship Gala",
                     host={"name": "Lake Tahoe Community College Foundation", "kind": "nonprofit"})
        assert _run(mod, conn, monkeypatch, uid) == 0
        assert _host(conn, uid)["derived_host_kind"] == "community"
        assert _count(conn, "communities") == 1
        assert not _activity(conn, "host-is-venue")


class TestVenueNameHelpers:
    def test_cached_live_venues_reads_a_stale_cache_and_never_fetches(self, tmp_path):
        path = tmp_path / "live-venues.json"
        assert cached_live_venues(path) == []
        path.write_text(json.dumps({"fetched_at": 0, "venues": [CASCADE]}))
        assert cached_live_venues(path) == [CASCADE]
        path.write_text("not json")
        assert cached_live_venues(path) == []

    def test_own_venue_match_names_the_bound_live_venue(self, fresh_db):
        fresh_db.row_factory = sqlite3.Row
        vuid = mirror_live_entity(fresh_db, "venue", CASCADE)
        event = {"venue_raw": "Cascade Kitchens", "derived_venue_uid": vuid}
        assert own_venue_named(fresh_db, "Cascade Kitchens Food Hall & Bar", event) == \
            VenueNameMatch("Cascade Kitchens", "cascade-kitchens", "own")
        assert own_venue_named(fresh_db, "Rotary Club of Tahoe", event) is None

    def test_other_venue_match_prefers_local_then_live(self, fresh_db):
        fresh_db.row_factory = sqlite3.Row
        assert other_venue_named(fresh_db, "Alibi Ale Works", [ALIBI]) == \
            VenueNameMatch("Alibi Ale Works", "alibi-ale-works", "live")
        mirror_live_entity(fresh_db, "venue", ALIBI)
        assert other_venue_named(fresh_db, "Alibi Ale Works", []).where == "local"
