"""Event times: extraction is in the town's timezone, the DB carries UTC, and
ship only ever sends UTC start/end times."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from engine.event_extract import build_extraction_prompt, event_uid, upsert_event


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


@pytest.fixture
def new_york(monkeypatch):
    monkeypatch.setenv("AGENT_TZ", "America/New_York")


def _times(conn, title, date):
    row = conn.execute(
        "SELECT date_utc, end_at_utc FROM events WHERE event_uid = ?",
        (event_uid("Seed", title, date),),
    ).fetchone()
    return row["date_utc"], row["end_at_utc"]


class TestUpsertSetsUtc:
    def test_insert_converts_local_times_in_town_tz(self, conn):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                    "end_at": "2026-10-09T23:00:00"})
        assert _times(conn, "Jazz Night", "2026-10-09T20:00:00") == (
            "2026-10-10T03:00:00+00:00", "2026-10-10T06:00:00+00:00")

    def test_explicit_offset_is_respected(self, conn):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00-04:00"})
        assert _times(conn, "Jazz Night", "2026-10-09T20:00:00-04:00") == (
            "2026-10-10T00:00:00+00:00", None)

    def test_update_recomputes_utc(self, conn):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                    "end_at": "2026-10-09T23:00:00"})
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                    "description": "now with a description"})
        assert _times(conn, "Jazz Night", "2026-10-09T20:00:00") == (
            "2026-10-10T03:00:00+00:00", None)

    def test_unchanged_backfills_missing_utc(self, conn):
        ev = {"title": "Jazz Night", "date": "2026-10-09T20:00:00"}
        upsert_event(conn, "Seed", ev)
        conn.execute("UPDATE events SET date_utc = NULL")  # row from before UTC columns were set
        assert upsert_event(conn, "Seed", ev) == "unchanged"
        assert _times(conn, "Jazz Night", "2026-10-09T20:00:00")[0] == "2026-10-10T03:00:00+00:00"

    def test_unparseable_date_leaves_utc_null(self, conn):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "TBD"})
        assert _times(conn, "Jazz Night", "TBD") == (None, None)

    @pytest.mark.parametrize("date", ["Saturday", "7pm", "TBD 2026", "Sept 9th-12th"])
    def test_non_iso_date_is_not_guessed(self, conn, date):
        # dateutil would fill the missing parts from today — never ship that.
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": date})
        assert _times(conn, "Jazz Night", date)[0] is None

    def test_end_not_after_start_is_dropped(self, conn):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                    "end_at": "2026-10-09T01:00:00"})
        assert _times(conn, "Jazz Night", "2026-10-09T20:00:00") == (
            "2026-10-10T03:00:00+00:00", None)

    @pytest.mark.parametrize("local, utc", [
        # America/New_York: DST starts 2026-03-08, ends 2026-11-01.
        ("2026-03-07T12:00:00", "2026-03-07T17:00:00+00:00"),
        ("2026-03-08T12:00:00", "2026-03-08T16:00:00+00:00"),
        ("2026-10-31T20:00:00", "2026-11-01T00:00:00+00:00"),
        ("2026-11-01T20:00:00", "2026-11-02T01:00:00+00:00"),
    ])
    def test_non_pacific_town_across_dst(self, conn, new_york, local, utc):
        upsert_event(conn, "Seed", {"title": "Fair", "date": local})
        assert _times(conn, "Fair", local)[0] == utc


class TestPrompt:
    def test_prompt_uses_town_timezone(self, new_york):
        prompt = build_extraction_prompt({"name": "Saratoga"}, "Seed", "https://x.example", "md")
        assert "America/New_York" in prompt
        assert "Los_Angeles" not in prompt


# --------------------------------------------------------------------------
# event-ship
# --------------------------------------------------------------------------

def _load_ship():
    path = Path(__file__).resolve().parent.parent / "skills/business/event-ship/ship.py"
    spec = importlib.util.spec_from_file_location("event_ship", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    calls: list = []

    def __init__(self):
        FakeClient.calls = []

    def create(self, resource, payload, idempotency_key=None):
        FakeClient.calls.append((resource, payload))
        return {"slug": payload.get("slug")}


@pytest.fixture
def ship(conn, monkeypatch):
    mod = _load_ship()
    monkeypatch.setattr(mod, "open_db", lambda: conn)
    for k, v in (("TOWNLOOPS_API_URL", "https://tl.example"), ("TOWNLOOPS_API_KEY", "k"),
                 ("TOWNLOOPS_TOWN_SLUG", "testtown")):
        monkeypatch.setenv(k, v)
    import engine.townloops_api as api
    monkeypatch.setattr(api, "Client", FakeClient)
    FakeClient.calls = []
    return mod


def _run(ship, monkeypatch, uid):
    monkeypatch.setattr(ship, "parse_event_uid_arg", lambda: uid)
    return ship.main()


class TestShipSendsUtc:
    def test_payload_uses_utc_columns(self, conn, ship, monkeypatch):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                    "end_at": "2026-10-09T23:00:00"})
        assert _run(ship, monkeypatch, event_uid("Seed", "Jazz Night", "2026-10-09T20:00:00")) == 0
        [(resource, payload)] = FakeClient.calls
        assert resource == "events"
        assert payload["start_at"] == "2026-10-10T03:00:00+00:00"
        assert payload["end_at"] == "2026-10-10T06:00:00+00:00"

    def test_no_utc_start_is_not_shipped(self, conn, ship, monkeypatch):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "TBD"})
        uid = event_uid("Seed", "Jazz Night", "TBD")
        assert _run(ship, monkeypatch, uid) != 0
        assert FakeClient.calls == []
        row = conn.execute("SELECT pushed_at, townloops_slug FROM events WHERE event_uid = ?",
                           (uid,)).fetchone()
        assert row["pushed_at"] is None and row["townloops_slug"] is None
        flagged = conn.execute(
            "SELECT error FROM agent_activity WHERE worker = 'event-ship' AND target = ? AND ok = 0",
            (uid,)).fetchone()
        assert flagged is not None

    def test_row_queued_before_utc_columns_converts_in_town_tz(self, conn, ship, monkeypatch, new_york):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00"})
        conn.execute("UPDATE events SET date_utc = NULL, end_at_utc = NULL")
        assert _run(ship, monkeypatch, event_uid("Seed", "Jazz Night", "2026-10-09T20:00:00")) == 0
        [(_, payload)] = FakeClient.calls
        assert payload["start_at"] == "2026-10-10T00:00:00+00:00"

    def test_naive_end_without_utc_is_dropped(self, conn, ship, monkeypatch):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                    "end_at": "late"})
        assert _run(ship, monkeypatch, event_uid("Seed", "Jazz Night", "2026-10-09T20:00:00")) == 0
        [(_, payload)] = FakeClient.calls
        assert "end_at" not in payload
