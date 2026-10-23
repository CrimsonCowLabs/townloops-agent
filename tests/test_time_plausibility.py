"""engine/time_plausibility.py — an implausible time is held for review (#46).

SLT shipped `sidewalk-sound-waves-dj-montague-2026-07-03` at Fri 05:00 →
21:00: AleWorX published "5:00 am to 9:00 pm", almost certainly meaning
5 PM. Extraction was faithful; the calendar entry was still wrong. An event
whose times are wrong on their face — a pre-dawn start, a duration no event
runs, an end before its start — is held for the operator in both modes
(pending, never shipped, never dropped): an approval after the hold ships it
as is, so the early races SLT really carries (Tahoe 100 at 05:30) still go.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from engine import autonomy, event_detail, event_state, time_plausibility as tp
from engine.event_extract import event_uid, upsert_event
from tests.autonomy_town import PROFILE, Town, soon


ROOT = Path(__file__).resolve().parent.parent
SEED = "AleWorX"
SIDEWALK = {"title": "Sidewalk Sound Waves: DJ Montague", "date": "2026-07-03T05:00:00",
            "end_at": "2026-07-03T21:00:00", "venue": "AleWorX"}
TAHOE_100 = {"title": "Tahoe 100", "date": "2099-07-18T05:30:00", "venue": "Kings Beach"}
JAZZ = {"title": "Jazz Night", "date": "2099-07-03T19:00:00", "end_at": "2099-07-03T22:00:00",
        "venue": "Riva Grill"}


def rules(start, end=None) -> list[str]:
    return [t.rule for t in tp.check(start, end)]


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------

class TestPreDawn:
    @pytest.mark.parametrize("start", ["2026-07-03T01:00:00", "2026-07-03T05:00:00",
                                       "2026-07-03T05:30:00", "2026-07-03T05:59:00"])
    def test_a_start_from_one_until_six_is_held(self, start):
        assert rules(start) == [tp.PRE_DAWN]

    @pytest.mark.parametrize("start", ["2026-07-03T06:00:00", "2026-07-03T00:30:00",
                                       "2026-07-03T00:59:00", "2026-07-03T23:30:00"])
    def test_six_oclock_and_after_midnight_ship(self, start):
        assert rules(start) == []

    def test_local_midnight_is_the_date_only_convention_not_a_time(self):
        assert rules("2026-07-03T00:00:00", "2026-07-05T23:59:59") == []
        assert rules("2026-07-03") == []

    def test_a_start_with_an_offset_is_read_in_the_towns_timezone(self):
        # 12:00 UTC is 05:00 in South Lake Tahoe in July.
        assert rules("2026-07-03T12:00:00+00:00") == [tp.PRE_DAWN]
        assert rules("2026-07-03T19:00:00+00:00") == []

    def test_the_reason_names_the_time_and_the_window(self):
        [trip] = tp.check("2026-07-18T05:30:00", None)
        assert "05:30" in trip.reason and "01:00" in trip.reason and "06:00" in trip.reason


class TestEnd:
    def test_an_end_before_its_start_is_held(self):
        assert rules("2026-07-03T19:00:00", "2026-07-03T18:00:00") == [tp.ENDS_BEFORE_START]

    def test_an_overnight_end_not_rolled_to_the_next_day_is_held(self):
        assert rules("2026-07-03T21:00:00", "2026-07-03T01:00:00") == [tp.ENDS_BEFORE_START]

    def test_an_overnight_end_on_the_next_day_ships(self):
        assert rules("2026-07-03T21:00:00", "2026-07-04T01:00:00") == []
        assert rules("2026-07-03T19:00:00", "2026-07-04T00:00:00") == []

    def test_the_longest_plausible_day_ships_and_a_minute_more_is_held(self):
        assert rules("2026-07-04T08:00:00", "2026-07-04T22:00:00") == []
        [trip] = tp.check("2026-07-04T08:00:00", "2026-07-04T22:01:00")
        assert trip.rule == tp.TOO_LONG and "14h" in trip.reason

    def test_a_multi_day_span_with_times_is_held(self):
        assert rules("2026-10-16T10:00:00", "2026-10-18T17:00:00") == [tp.TOO_LONG]

    def test_an_end_of_the_starts_own_day_without_a_time_says_nothing(self):
        assert rules("2026-07-03T19:00:00", "2026-07-03") == []
        assert rules("2026-07-03T19:00:00", "2026-07-03T00:00:00") == []

    def test_a_date_only_start_has_no_times_to_doubt(self):
        assert rules("2026-07-03", "2026-07-01T05:00:00") == []

    def test_no_end_or_an_unreadable_one_is_no_end(self):
        assert rules("2026-07-03T19:00:00", None) == []
        assert rules("2026-07-03T19:00:00", "late") == []

    def test_the_slt_sidewalk_event_trips_both_rules_it_breaks(self):
        trips = tp.check(SIDEWALK["date"], SIDEWALK["end_at"])
        assert [t.rule for t in trips] == [tp.PRE_DAWN, tp.TOO_LONG]


# --------------------------------------------------------------------------
# The hold
# --------------------------------------------------------------------------

@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


def row(conn, uid) -> dict:
    return dict(conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())


def check_of(conn, uid) -> dict:
    return json.loads(row(conn, uid)["extra_json"] or "{}").get("time_check") or {}


def found(conn, evt) -> str:
    """event-find's write of one extraction."""
    upsert_event(conn, SEED, evt)
    uid = event_uid(SEED, evt["title"], evt["date"])
    tp.record(conn, uid)
    return uid


class TestRecord:
    def test_an_implausible_extraction_is_held_naming_the_rule(self, conn):
        uid = found(conn, SIDEWALK)
        r = row(conn, uid)
        assert (r["state"], r["approval_status"]) == ("metadata-extracted", "pending")
        tc = check_of(conn, uid)
        assert tc["status"] == tp.HELD_FOR_REVIEW
        assert tc["rules"] == [tp.PRE_DAWN, tp.TOO_LONG]
        assert r["approval_note"].startswith(tp.NOTE_PREFIX) and "05:00" in r["approval_note"]

    def test_a_plausible_extraction_is_not(self, conn):
        uid = found(conn, JAZZ)
        assert check_of(conn, uid) == {}
        assert not row(conn, uid)["approval_note"]

    def test_a_source_that_corrects_its_times_releases_the_hold(self, conn):
        uid = found(conn, {**JAZZ, "end_at": "2099-07-03T18:00:00"})
        assert check_of(conn, uid)["rules"] == [tp.ENDS_BEFORE_START]
        found(conn, JAZZ)
        assert check_of(conn, uid) == {}
        assert not row(conn, uid)["approval_note"]

    def test_another_note_is_kept_beside_the_hold(self, conn):
        upsert_event(conn, SEED, SIDEWALK)
        uid = event_uid(SEED, SIDEWALK["title"], SIDEWALK["date"])
        conn.execute("UPDATE events SET approval_note = '[reconcile] live event x changed: date' "
                     "WHERE event_uid = ?", (uid,))
        tp.record(conn, uid)
        note = row(conn, uid)["approval_note"]
        assert note.startswith("[reconcile] live event x changed") and tp.NOTE_PREFIX in note

    def test_shipped_and_declined_rows_are_left_alone(self, conn):
        upsert_event(conn, SEED, SIDEWALK)
        uid = event_uid(SEED, SIDEWALK["title"], SIDEWALK["date"])
        conn.execute("UPDATE events SET state = 'shipped' WHERE event_uid = ?", (uid,))
        tp.record(conn, uid)
        assert check_of(conn, uid) == {} and row(conn, uid)["state"] == "shipped"

    def test_an_approved_event_whose_end_moves_is_sent_back(self, conn):
        uid = found(conn, JAZZ)
        assert event_state.approve(conn, uid)
        found(conn, {**JAZZ, "end_at": "2099-07-05T22:00:00"})
        r = row(conn, uid)
        assert (r["state"], r["approval_status"], r["approval_at"]) == (
            "metadata-extracted", "pending", None)
        assert check_of(conn, uid)["rules"] == [tp.TOO_LONG]


class TestOperator:
    def test_an_approval_after_the_hold_accepts_those_times(self, conn):
        uid = found(conn, TAHOE_100)
        assert event_state.approve(conn, uid)
        assert not tp.gate(conn, row(conn, uid))
        assert check_of(conn, uid)["status"] == tp.ACCEPTED
        assert row(conn, uid)["state"] == "approved"
        # A re-read of the same times stands accepted.
        found(conn, TAHOE_100)
        assert row(conn, uid)["state"] == "approved"

    def test_an_accepted_event_whose_times_change_is_held_again(self, conn):
        uid = found(conn, TAHOE_100)
        assert event_state.approve(conn, uid)
        tp.gate(conn, row(conn, uid))
        found(conn, {**TAHOE_100, "end_at": "2099-07-19T15:00:00"})
        assert row(conn, uid)["state"] == "metadata-extracted"
        assert check_of(conn, uid)["status"] == tp.HELD_FOR_REVIEW

    def test_an_approval_before_the_hold_is_not_an_acceptance(self, conn):
        upsert_event(conn, SEED, TAHOE_100)
        uid = event_uid(SEED, TAHOE_100["title"], TAHOE_100["date"])
        assert event_state.approve(conn, uid)   # approved before the rule existed
        g = tp.gate(conn, row(conn, uid))
        assert g.held and "05:30" in g.reasons[0]
        r = row(conn, uid)
        assert (r["state"], r["approval_status"]) == ("metadata-extracted", "pending")
        assert check_of(conn, uid)["status"] == tp.HELD_FOR_REVIEW

    def test_a_decline_ends_it(self, conn):
        uid = found(conn, TAHOE_100)
        assert [h["event_uid"] for h in tp.held_for_review(conn)] == [uid]
        assert event_state.decline(conn, uid, "not real")
        assert tp.held_for_review(conn) == []


class TestReadDetail:
    def test_an_approved_implausible_event_is_held_before_its_page_is_read(self, conn):
        upsert_event(conn, SEED, {**TAHOE_100, "source_url": "https://ex.example/t100"})
        uid = event_uid(SEED, TAHOE_100["title"], TAHOE_100["date"])
        assert event_state.approve(conn, uid)
        fetched = []
        result = event_detail.read_detail(conn, row(conn, uid), final_attempt=False,
                                          fetch=lambda u: fetched.append(u) or (True, "", ""),
                                          llm=lambda p, timeout_s=0: (True, "{}", ""))
        assert result.status == "held" and fetched == []
        assert row(conn, uid)["state"] == "metadata-extracted"

    def test_a_hold_that_lost_its_race_is_deferred_not_read(self, conn, monkeypatch):
        upsert_event(conn, SEED, {**TAHOE_100, "source_url": "https://ex.example/t100"})
        uid = event_uid(SEED, TAHOE_100["title"], TAHOE_100["date"])
        assert event_state.approve(conn, uid)
        # Another worker or the operator moved it first: the hold is a no-op.
        monkeypatch.setattr(event_state, "reopen_for_review", lambda *a, **k: False)
        fetched = []
        result = event_detail.read_detail(conn, row(conn, uid), final_attempt=False,
                                          fetch=lambda u: fetched.append(u) or (True, "", ""),
                                          llm=lambda p, timeout_s=0: (True, "{}", ""))
        assert result.status == "deferred" and fetched == []


class TestAutonomy:
    def test_the_verdict_is_autonomys(self):
        evidence = {"rules": [tp.PRE_DAWN], "reasons": ["starts 05:30"]}
        assert autonomy.judge_times(evidence, autonomy.Settings(mode="autonomous")).verdict \
            == "hold"
        assert autonomy.judge_times(evidence, autonomy.Settings()).verdict == autonomy.AWAIT
        assert autonomy.judge_times({"rules": []}, autonomy.Settings()).verdict == "ship"


# --------------------------------------------------------------------------
# A whole town
# --------------------------------------------------------------------------

ARENA = "https://arena.example/events"


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


def race(title="Tahoe 100", hour=5, minute=30, **kw):
    d = soon(14, hour)[:11] + f"{hour:02d}:{minute:02d}:00"
    return {"title": title, "date": d, "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "A race.",
            "categories": ["community"], "tags": [], "source_url": None, **kw}


def autonomy_skill(conn, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "autonomy_skill_times", ROOT / "skills/business/autonomy/autonomy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "open_db", lambda: conn)
    return mod


class TestTown:
    def test_autonomous_holds_it_until_the_operator_accepts_then_it_ships(
            self, town, monkeypatch):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [race(), race("Big Chief 50K", hour=6, minute=0)])
        town.run()
        assert town.submitted_titles() == ["Big Chief 50K"]
        assert town.reasons()["Tahoe 100"] == "implausible-time"

        _, report, telegram = town.digest()
        assert "## Held — implausible time" in report and "Tahoe 100" in report
        assert "pre-dawn" in report.lower() or "05:30" in report
        assert "held for an implausible time: 1" in telegram

        [held] = tp.held_for_review(town.conn)
        mod = autonomy_skill(town.conn, monkeypatch)
        assert mod.main(["accept", held["event_uid"][:10]]) == 0
        town.drain_all()
        assert town.submitted_titles() == ["Big Chief 50K", "Tahoe 100"]
        assert tp.held_for_review(town.conn) == []

    def test_supervised_holds_it_with_the_rule_on_the_approval_queue(self, town):
        town.set_profile({**PROFILE, "operating_mode": "supervised"})
        town.seed("Arena", ARENA)
        town.listing(ARENA, [race()])
        town.run()
        r = dict(town.conn.execute("SELECT * FROM events").fetchone())
        assert (r["state"], r["approval_status"]) == ("metadata-extracted", "pending")
        assert tp.NOTE_PREFIX in r["approval_note"] and "05:30" in r["approval_note"]
        # The operator approves it on the dashboard: it ships as is.
        assert event_state.approve(town.conn, r["event_uid"])
        town.conn.commit()
        town.drain_all()
        assert [p["title"] for p in town.submitted_events()] == ["Tahoe 100"]

    def test_ship_refuses_an_implausible_event_approved_before_the_hold(self, town):
        town.seed("Arena", ARENA)
        upsert_event(town.conn, "Arena", race())
        uid = event_uid("Arena", "Tahoe 100", race()["date"])
        town.conn.execute("UPDATE events SET state = 'entities-enriched', "
                          "  approval_status = 'approved', approval_at = '2026-01-01' "
                          "WHERE event_uid = ?", (uid,))
        from engine import workqueue
        workqueue.enqueue(town.conn, uid, "ship")
        town.conn.commit()
        town.drain_all()
        assert town.submitted_events() == []
        r = dict(town.conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())
        assert (r["state"], r["approval_status"]) == ("metadata-extracted", "pending")
        assert town.reasons()["Tahoe 100"] == "implausible-time"

    def test_a_ship_hold_that_lost_its_race_is_retried_never_marked_shipped(
            self, town, monkeypatch):
        town.seed("Arena", ARENA)
        upsert_event(town.conn, "Arena", race())
        uid = event_uid("Arena", "Tahoe 100", race()["date"])
        town.conn.execute("UPDATE events SET state = 'entities-enriched', "
                          "  approval_status = 'approved', approval_at = '2026-01-01' "
                          "WHERE event_uid = ?", (uid,))
        from engine import workqueue
        workqueue.enqueue(town.conn, uid, "ship")
        town.conn.commit()
        # The hold can't move it (a concurrent operator action won the race).
        monkeypatch.setattr(event_state, "reopen_for_review", lambda *a, **k: False)
        town.drain()
        assert town.submitted_events() == []
        r = dict(town.conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())
        assert r["state"] == "entities-enriched"   # not advanced to shipped unsent
        q = town.conn.execute("SELECT attempts, resolved_at, claimed_at FROM event_workqueue "
                              "WHERE event_uid = ? AND next_action = 'ship'", (uid,)).fetchone()
        assert tuple(q) == (0, None, None)          # released for a retry, no attempt spent
