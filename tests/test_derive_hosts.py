"""A venue is never an event's host business or community (issue #4): a host
name that is the event's own venue, or any local or live venue, derives no
host — the venue link already carries it."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from engine import workqueue
from engine.derive_helpers import mirror_live_entity
from engine.event_extract import event_uid, upsert_event


CASCADE = {"id": 41, "slug": "cascade-kitchens", "name": "Cascade Kitchens",
           "address": "3668 Lake Tahoe Blvd"}
TIPSY = {"id": 99, "slug": "tipsy-putt", "name": "Tipsy Putt"}


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


def _run(mod, conn, monkeypatch, uid, live=lambda: []):
    monkeypatch.setattr(mod, "open_db", lambda: conn)
    monkeypatch.setattr(mod, "parse_event_uid_arg", lambda: uid)
    monkeypatch.setenv("TOWNLOOPS_API_KEY", "k")
    monkeypatch.setattr(mod, "load_live_venues", lambda fetch: live())
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


class TestVenueIsNeverTheHost:
    def test_cascade_kitchens_organizer_is_its_own_venue(self, derive, monkeypatch):
        mod, conn = derive
        vuid = mirror_live_entity(conn, "venue", CASCADE)
        uid = _event(conn, "Cascade Kitchens", venue_uid=vuid,
                     host={"name": "Cascade Kitchens Food Hall & Bar", "kind": "business"})
        assert _run(mod, conn, monkeypatch, uid, lambda: [CASCADE]) == 0
        assert tuple(_host(conn, uid)) == (None, None)
        assert _count(conn, "businesses") == 0
        assert conn.execute("SELECT COUNT(*) FROM linked_events WHERE role = 'host'").fetchone()[0] == 0
        [detail] = _activity(conn, "host-is-venue")
        assert "Cascade Kitchens" in detail

    def test_own_venue_matches_without_a_live_lookup(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Cascade Kitchens",
                     host={"name": "Cascade Kitchens Food Hall & Bar", "kind": "business"})

        def no_lookup():
            raise AssertionError("live list fetched")
        assert _run(mod, conn, monkeypatch, uid, no_lookup) == 0
        assert _host(conn, uid)["derived_host_uid"] is None
        assert _count(conn, "businesses") == 0

    def test_host_matching_another_local_venue_is_skipped_and_logged(self, derive, monkeypatch):
        mod, conn = derive
        mirror_live_entity(conn, "venue", CASCADE)
        uid = _event(conn, "Lakeview Commons", title="Chef Pop-up",
                     host={"name": "Cascade Kitchens Food Hall & Bar", "kind": "business"})
        assert _run(mod, conn, monkeypatch, uid) == 0
        assert _host(conn, uid)["derived_host_uid"] is None
        assert _count(conn, "businesses") == 0
        [detail] = _activity(conn, "host-is-venue")
        assert "Cascade Kitchens" in detail and "cascade-kitchens" in detail

    def test_host_matching_a_live_venue_is_skipped_and_logged(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Lakeview Commons", title="Chef Pop-up",
                     host={"name": "Cascade Kitchens Food Hall & Bar", "kind": "business"})
        assert _run(mod, conn, monkeypatch, uid, lambda: [TIPSY, CASCADE]) == 0
        assert _host(conn, uid)["derived_host_uid"] is None
        assert _count(conn, "businesses") == 0
        [detail] = _activity(conn, "host-is-venue")
        assert "live venue cascade-kitchens" in detail

    def test_preset_host_that_is_a_venue_is_dropped(self, derive, monkeypatch):
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

    def test_live_lookup_failure_retries(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Lakeview Commons", title="Chef Pop-up",
                     host={"name": "Alibi Ale Works", "kind": "business"})
        workqueue.enqueue(conn, uid, "extract-hosts")

        def down():
            raise RuntimeError("503")
        assert _run(mod, conn, monkeypatch, uid, down) == 1
        assert _count(conn, "businesses") == 0


class TestGenuineHostsStillDerive:
    def test_club_at_a_venue_is_a_community_host(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Cascade Kitchens", title="Rotary Lunch",
                     host={"name": "Rotary Club of Tahoe", "kind": "community"})
        assert _run(mod, conn, monkeypatch, uid, lambda: [CASCADE]) == 0
        assert _host(conn, uid)["derived_host_kind"] == "community"
        assert _count(conn, "communities") == 1

    def test_brewery_that_is_not_the_venue_is_a_business_host(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Cascade Kitchens", title="Tap Takeover",
                     host={"name": "Alibi Ale Works", "kind": "business"})
        assert _run(mod, conn, monkeypatch, uid, lambda: [TIPSY, CASCADE]) == 0
        assert _host(conn, uid)["derived_host_kind"] == "business"
        assert _count(conn, "businesses") == 1

    def test_foundation_named_after_its_venue_is_still_a_host(self, derive, monkeypatch):
        mod, conn = derive
        uid = _event(conn, "Lake Tahoe Community College", title="Scholarship Gala",
                     host={"name": "Lake Tahoe Community College Foundation", "kind": "nonprofit"})
        assert _run(mod, conn, monkeypatch, uid) == 0
        assert _host(conn, uid)["derived_host_kind"] == "community"
        assert _count(conn, "communities") == 1
