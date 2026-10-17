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

    def test_explicit_recurrence_expands_within_the_horizon(self, new_york):
        """A source's explicit rule ("every Thursday through December") becomes
        dated occurrences, one title and venue for all, so they auto-link into
        one TownLoops Series; vague recurrence never does."""
        from engine.event_extract import RECURRENCE_HORIZON_DAYS
        prompt = build_extraction_prompt({"name": "Saratoga"}, "Seed", "https://x.example", "md")
        assert f"next {RECURRENCE_HORIZON_DAYS} days" in prompt
        assert "identical title and venue" in prompt
        assert "explicit" in prompt and "never invent" in prompt.lower()


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

    def get(self, resource, slug):  # nothing is live yet
        from engine.townloops_api import TownloopsError
        raise TownloopsError(404, "not_found", slug)

    def list(self, resource, **params):
        return []

    def paginate(self, resource, **params):  # no Series, nothing live
        return iter([])


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


# --------------------------------------------------------------------------
# Published times only (#44): a time the source doesn't state is never shipped
# --------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "times"
SAVE_THE_DATE = (FIXTURES / "save_the_date.md").read_text()
TIMED = (FIXTURES / "timed.md").read_text()

# What an extraction answered for the live `labor-day-cruise-2026-09-07`: a
# clock time and an exact two-hour end the page never stated.
INVENTED = ('[{"title": "Labor Day Cruise", "date": "2026-09-07T00:07:00", '
            '"end_at": "2026-09-07T02:07:00", "venue": "Zephyr Cove Resort"}]')


class TestPublishedTimes:
    def _one(self, page, date, end=None):
        from engine.event_extract import published_times
        return published_times({"title": "Cruise", "date": date, "end_at": end}, page)

    def test_a_time_the_page_does_not_state_becomes_date_only(self):
        evt = self._one(SAVE_THE_DATE, "2026-09-07T00:07:00", "2026-09-07T02:07:00")
        assert (evt["date"], evt["end_at"]) == ("2026-09-07", None)

    def test_invented_midnight_is_date_only_too(self):
        evt = self._one(SAVE_THE_DATE, "2026-09-07T00:00:00")
        assert evt["date"] == "2026-09-07"

    @pytest.mark.parametrize("date", ["2026-09-07T09:07:00", "2026-09-07T07:00:00",
                                      "2026-09-07T04:49:00", "2026-09-07T20:26:00"])
    def test_digits_that_are_not_a_clock_time_do_not_count(self, date):
        # "09.07.2026", the phone number and the street number are on the page.
        assert self._one(SAVE_THE_DATE, date)["date"] == "2026-09-07"

    def test_a_stated_start_and_end_are_kept(self):
        evt = self._one(TIMED, "2026-09-07T10:00:00", "2026-09-07T12:00:00")
        assert (evt["date"], evt["end_at"]) == ("2026-09-07T10:00:00", "2026-09-07T12:00:00")

    def test_an_unstated_end_is_dropped_but_the_start_kept(self):
        evt = self._one(TIMED, "2026-09-07T10:00:00", "2026-09-07T14:00:00")
        assert (evt["date"], evt["end_at"]) == ("2026-09-07T10:00:00", None)

    def test_a_date_only_start_has_no_end(self):
        evt = self._one(TIMED, "2026-09-07", "2026-09-07T12:00:00")
        assert (evt["date"], evt["end_at"]) == ("2026-09-07", None)
        evt = self._one("Sept 5–7, 2026", "2026-09-05", "2026-09-07")
        assert (evt["date"], evt["end_at"]) == ("2026-09-05", None)

    @pytest.mark.parametrize("page, date", [
        ("Doors at 7pm", "2026-09-07T19:00:00"),
        ("Doors at 7 p.m.", "2026-09-07T19:00:00"),
        ("Starts 7:30PM sharp", "2026-09-07T19:30:00"),
        ("7–9pm on the plaza", "2026-09-07T19:00:00"),
        ("7 to 9 pm on the plaza", "2026-09-07T19:00:00"),
        ("10:30-11:30am", "2026-09-07T10:30:00"),
        ("Showtime 19:00", "2026-09-07T19:00:00"),
        ("Showtime 7:00", "2026-09-07T19:00:00"),
        ("Lunch at noon", "2026-09-07T12:00:00"),
        ("Countdown at midnight", "2026-09-07T00:00:00"),
        ("Opens 12 AM", "2026-09-07T00:00:00"),
        ("Opens 9a", "2026-09-07T09:00:00"),
        ('"startDate": "2026-09-07T19:00:00"', "2026-09-07T19:00:00"),
    ])
    def test_published_forms_count(self, page, date):
        assert self._one(page, date)["date"] == date

    @pytest.mark.parametrize("page, date", [
        ("Doors at 7pm", "2026-09-07T07:00:00"),         # wrong meridiem
        ("Doors at 7pm", "2026-09-07T19:30:00"),         # wrong minute
        ("Room 7, 2026 season", "2026-09-07T07:00:00"),  # a bare number
    ])
    def test_a_different_time_does_not_count(self, page, date):
        assert self._one(page, date)["date"] == "2026-09-07"

    def test_non_iso_dates_are_left_for_upsert_to_reject(self):
        assert self._one(SAVE_THE_DATE, "TBD")["date"] == "TBD"

    def test_extracted_events_applies_the_rule(self):
        from engine.event_provenance import listing_events
        [evt] = listing_events(INVENTED, SAVE_THE_DATE, "https://zephyrcove.example/events")
        assert (evt["date"], evt["end_at"]) == ("2026-09-07", None)

    def test_prompt_asks_for_a_date_alone_when_no_time_is_stated(self):
        prompt = build_extraction_prompt({"name": "Tahoe"}, "Seed", "https://x.example", "md")
        assert '"2026-08-15"' in prompt and "never guess" in prompt.lower()


class TestDateOnlyUtc:
    def test_date_only_start_is_town_local_midnight_with_no_end(self):
        from engine.event_extract import utc_times
        assert utc_times("2026-09-07", "2026-09-07T02:07:00") == (
            "2026-09-07T07:00:00+00:00", None)

    def test_date_only_start_in_another_town(self, new_york):
        from engine.event_extract import utc_times
        assert utc_times("2026-09-07", None) == ("2026-09-07T04:00:00+00:00", None)


class TestShipsDateOnly:
    """The fixtures through extraction → upsert → event-ship's payload."""

    def _ship(self, conn, ship, monkeypatch, page, llm_out):
        from engine.event_provenance import listing_events
        [evt] = listing_events(llm_out, page, "https://zephyrcove.example/events")
        upsert_event(conn, "Zephyr Cove", evt)
        assert _run(ship, monkeypatch, event_uid("Zephyr Cove", evt["title"], evt["date"])) == 0
        [(_, payload)] = FakeClient.calls
        return payload

    def test_save_the_date_ships_at_local_midnight_with_no_end(self, conn, ship, monkeypatch):
        payload = self._ship(conn, ship, monkeypatch, SAVE_THE_DATE, INVENTED)
        assert payload["start_at"] == "2026-09-07T07:00:00+00:00"   # 00:00 PDT
        assert "end_at" not in payload

    def test_a_stated_time_ships_unchanged(self, conn, ship, monkeypatch):
        payload = self._ship(conn, ship, monkeypatch, TIMED, (
            '[{"title": "Labor Day Cruise", "date": "2026-09-07T10:00:00", '
            '"end_at": "2026-09-07T12:00:00"}]'))
        assert payload["start_at"] == "2026-09-07T17:00:00+00:00"
        assert payload["end_at"] == "2026-09-07T19:00:00+00:00"

    def test_a_row_extracted_before_the_rule_is_not_the_one_re_found(
            self, conn, ship, monkeypatch):
        """labor-day-cruise-2026-09-07 re-ingested today: the old row with the
        invented time is left behind; the date-only row is the one shipped."""
        upsert_event(conn, "Zephyr Cove", {"title": "Labor Day Cruise",
                                           "date": "2026-09-07T00:07:00",
                                           "end_at": "2026-09-07T02:07:00"})
        payload = self._ship(conn, ship, monkeypatch, SAVE_THE_DATE, INVENTED)
        assert payload["start_at"] == "2026-09-07T07:00:00+00:00"
        assert "end_at" not in payload


    def test_a_date_only_row_with_a_stale_utc_end_ships_without_it(self, conn, ship, monkeypatch):
        upsert_event(conn, "Zephyr Cove", {"title": "Labor Day Cruise", "date": "2026-09-07"})
        conn.execute("UPDATE events SET end_at_utc = '2026-09-07T09:07:00+00:00'")
        assert _run(ship, monkeypatch, event_uid("Zephyr Cove", "Labor Day Cruise",
                                                 "2026-09-07")) == 0
        [(_, payload)] = FakeClient.calls
        assert payload["start_at"] == "2026-09-07T07:00:00+00:00"
        assert "end_at" not in payload

    def test_a_date_change_to_a_date_only_start_takes_no_end_from_live(self, ship):
        # Live 00:07–02:07 moved to a date-only day: its two hours aren't kept.
        assert ship._patched_end(None, "2026-09-07T07:07:00+00:00", "2026-09-07T09:07:00+00:00",
                                 "2026-09-08T07:00:00+00:00", date_only=True) is None
        assert ship._patched_end(None, "2026-09-07T07:07:00+00:00", "2026-09-07T09:07:00+00:00",
                                 "2026-09-08T17:00:00+00:00") == "2026-09-08T19:00:00+00:00"


class TestEventFindKeepsPublishedTimes:
    def test_find_stores_a_no_time_listing_date_only(self, fresh_db, tmp_state_dir, monkeypatch):
        spec = importlib.util.spec_from_file_location(
            "event_find_times", Path(__file__).resolve().parent.parent
            / "skills/business/event-find/find.py")
        find = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(find)
        monkeypatch.setattr(find, "_DB_PATH", tmp_state_dir / "townloops.db")
        monkeypatch.setattr("engine.inspection._DB_PATH", tmp_state_dir / "townloops.db")
        fresh_db.execute("INSERT INTO event_seeds (name, url, kind, status, first_seen_at, "
                         "last_seen_at) VALUES ('Zephyr Cove', 'https://cruise.example/events', "
                         "'venue', 'approved', '2026-10-01', '2026-10-01')")
        fresh_db.commit()
        monkeypatch.setattr(find, "_load_town_profile", lambda: {})
        monkeypatch.setattr(find, "_firecrawl_listing", lambda url: (True, SAVE_THE_DATE, ""))
        monkeypatch.setattr(find, "_build_extraction_prompt", lambda *a: "prompt")
        monkeypatch.setattr(find, "_codex_exec", lambda p: (True, INVENTED, ""))
        assert find.main([]) == 0
        rows = fresh_db.execute("SELECT date, end_at, date_utc, end_at_utc FROM events").fetchall()
        assert [tuple(r) for r in rows] == [
            ("2026-09-07", None, "2026-09-07T07:00:00+00:00", None)]
