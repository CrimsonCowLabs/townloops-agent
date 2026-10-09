"""entity-live-sync's Series pass, through the skill's `run` entry point
against the fake API: the Series a curator (or the backfill) later puts a
shipped event in reaches its local row, and an upcoming occurrence this agent
shipped fills its blank Series-shared fields from a sibling."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine.event_extract import event_uid, upsert_event
from tests.fake_townloops import FakeAPI


SERIES = [{"id": 11, "slug": "trivia-thursday", "title": "Trivia Thursday"}]


def at(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        microsecond=0).isoformat()


def live(slug, days, series_slug=None, **kw):
    return {"id": abs(hash(slug)) % 10000, "slug": slug, "title": "Trivia Thursday",
            "start_at": at(days), "end_at": None, "venue_id": None, "venue_slug": None,
            "source_url": None, "description": None, "category_slugs": [], "tags": [],
            "accessibility_notes": None, "series_slug": series_slug,
            "series_id": 11 if series_slug else None, **kw}


@pytest.fixture
def sync(fresh_db, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    path = Path(__file__).resolve().parent.parent / "skills/business/entity-live-sync/sync.py"
    spec = importlib.util.spec_from_file_location("entity_live_sync_series", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "open_db", lambda: fresh_db)
    for k, v in (("TOWNLOOPS_API_URL", "https://tl.example"), ("TOWNLOOPS_API_KEY", "k"),
                 ("TOWNLOOPS_TOWN_SLUG", "slt")):
        monkeypatch.setenv(k, v)

    def go(api):
        import engine.townloops_api as tl
        monkeypatch.setattr(tl, "Client", lambda: api)
        monkeypatch.setattr(sys, "argv", ["sync.py", "run"])
        assert mod.main() == 0

    def shipped(slug, days):
        ev = {"title": "Trivia Thursday", "date": at(days)[:19]}
        upsert_event(fresh_db, "Loft", ev)
        uid = event_uid("Loft", ev["title"], ev["date"])
        fresh_db.execute("UPDATE events SET townloops_slug = ?, pushed_at = ?, state = 'shipped', "
                         "date_utc = ? WHERE event_uid = ?", (slug, at(-1), at(days), uid))
        return uid

    go.shipped = shipped
    go.conn = fresh_db
    return go


def _series_of(conn, uid):
    return tuple(conn.execute("SELECT series_slug, series_id FROM events WHERE event_uid = ?",
                              (uid,)).fetchone())


class TestReread:
    def test_backfill_linking_reaches_local_rows(self, sync):
        api = FakeAPI([live("trivia-1", 3), live("trivia-2", 10)], series=SERIES)
        u1, u2 = sync.shipped("trivia-1", 3), sync.shipped("trivia-2", 10)
        sync(api)
        assert _series_of(sync.conn, u1) == (None, None)

        api.link("trivia-1", "trivia-thursday")
        api.link("trivia-2", "trivia-thursday")
        sync(api)
        assert _series_of(sync.conn, u1) == ("trivia-thursday", 11)
        assert _series_of(sync.conn, u2) == ("trivia-thursday", 11)

    def test_a_curator_unlinking_reaches_local_rows(self, sync):
        api = FakeAPI([live("trivia-1", 3, "trivia-thursday")], series=SERIES)
        uid = sync.shipped("trivia-1", 3)
        sync(api)
        api.link("trivia-1", None)
        sync(api)
        assert _series_of(sync.conn, uid) == (None, None)


class TestSeriesFill:
    def test_upcoming_shipped_occurrence_fills_blanks_from_a_sibling(self, sync):
        sibling = live("trivia-0", -6, "trivia-thursday", description="Six rounds, prizes.",
                       tags=["trivia", "free"])
        api = FakeAPI([sibling, live("trivia-1", 3, "trivia-thursday")], series=SERIES)
        uid = sync.shipped("trivia-1", 3)
        sync(api)
        assert api.events["trivia-1"]["description"] == "Six rounds, prizes."
        assert api.events["trivia-1"]["tags"] == ["trivia"]
        assert api.events["trivia-1"]["start_at"] == at(3)
        [detail] = [r["detail"] for r in sync.conn.execute(
            "SELECT detail FROM agent_activity WHERE action = 'series-fill' AND target = ?",
            (uid,))]
        assert "description" in detail

    def test_events_this_agent_did_not_ship_are_left_alone(self, sync):
        sibling = live("trivia-0", -6, "trivia-thursday", description="Six rounds, prizes.")
        api = FakeAPI([sibling, live("curator-made", 3, "trivia-thursday")], series=SERIES)
        sync(api)
        assert api.events["curator-made"]["description"] is None
        assert api.patches() == []
