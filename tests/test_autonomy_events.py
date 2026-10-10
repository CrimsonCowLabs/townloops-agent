"""Autonomous mode, events (issue #1): the agent judges what a seed page
lists, submits only real upcoming in-town events to TownLoops as pending,
and stops submitting when the gate says so. Driven through event-find and
the workqueue drain against the fake API (tests/autonomy_town.py)."""

from __future__ import annotations

import json

import pytest

from tests.autonomy_town import PROFILE, Town, soon
from tests.fake_townloops import FakeAPI

ARENA = "https://arena.example/events"


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = __import__("sqlite3").Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


def concert(title="Jazz Night", days=7, **kw):
    return {"title": title, "date": soon(days), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live jazz.",
            "categories": ["music"], "tags": [], "source_url": None, **kw}


class TestJudgingASeedPage:
    def test_only_real_upcoming_in_town_events_are_submitted(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [
            concert("Jazz Night"),
            concert("Old Show", days=-3),
            concert("Reno Rodeo", venue="Reno Events Center", location="Reno, NV 89501"),
            concert("Box Office Closed for Maintenance"),
            concert("Far Future Gala", days=400),
        ])
        town.set_profile(PROFILE, horizon_days=120)
        town.profile["zip_codes"] = ["96150"]
        town.set_profile(town.profile)
        town.run()

        assert town.submitted_titles() == ["Jazz Night"]
        assert town.reasons() == {
            "Jazz Night": "checks-passed", "Old Show": "past",
            "Reno Rodeo": "out-of-town", "Box Office Closed for Maintenance": "not-an-event",
            "Far Future Gala": "beyond-horizon"}
        [live] = town.api.events.values()
        assert live["status"] == "pending"

    def test_undecided_items_are_settled_by_quotes_from_the_page(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Trivia", venue=None, location=None),
                             concert("Mystery", venue=None, location=None)],
                     "Trivia every week at the Lake Tahoe Arena in South Lake Tahoe.")

        town.judge_by_title({
            "Trivia": {"is_event": True, "event_quote": "Trivia every week at the Lake Tahoe Arena",
                       "in_town": True, "town_quote": "Lake Tahoe Arena in South Lake Tahoe"},
            "Mystery": {"is_event": True, "event_quote": "A quote that is not on the page at all",
                        "in_town": True, "town_quote": "somewhere in South Lake Tahoe"}})
        town.run()

        assert town.submitted_titles() == ["Trivia"]
        assert town.reasons() == {"Trivia": "ai-verified", "Mystery": "insufficient-evidence"}

    def test_an_ai_outage_defers_rather_than_guessing(self, town, monkeypatch):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Trivia", venue=None, location=None)])
        monkeypatch.setattr(town, "judge", lambda p: (_ for _ in ()).throw(RuntimeError()))
        orig = town._codex
        monkeypatch.setattr(town, "_codex", lambda p, timeout_s=0: (False, "", "throttled")
                            if p.startswith("You are checking") else orig(p))
        town.run()
        assert town.submitted_events() == []
        row = town.conn.execute("SELECT state, approval_status FROM events").fetchone()
        assert tuple(row) == ("metadata-extracted", "pending")
        assert town.reasons()["Trivia"] == "ai-unavailable"

    def test_switching_to_autonomous_picks_up_events_waiting_for_approval(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        town.set_profile({**PROFILE, "operating_mode": "supervised"})
        town.run()
        assert town.submitted_events() == []
        town.set_profile(PROFILE)
        town.run()
        assert town.submitted_titles() == ["Jazz Night"]


class TestSupervisedMode:
    def test_nothing_is_submitted_without_local_approval(self, town):
        town.set_profile({**PROFILE, "operating_mode": "supervised"})
        town.api = FakeAPI()
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night"), concert("Old Show", days=-3)])
        town.run()
        assert town.submitted_events() == []
        assert town.decisions() == []
        states = {r[0] for r in town.conn.execute("SELECT state FROM events")}
        assert states == {"metadata-extracted"}


class TestVenues:
    def test_an_ambiguous_venue_is_submitted_with_the_name_only(self, town):
        town.api.venues.update({
            "gunbarrel-tavern": {"id": 7, "slug": "gunbarrel-tavern", "name": "Gunbarrel Tavern"},
            "gunbarrel-grill": {"id": 8, "slug": "gunbarrel-grill", "name": "Gunbarrel Grill"}})
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night", venue="Gunbarrel")])
        town.run()
        [sent] = town.submitted_events()
        assert sent["venue_name"] == "Gunbarrel" and "venue_id" not in sent
        [d] = town.decisions("venue")
        assert d["reason"] == "ambiguous-live-venue"

    def test_with_entity_moderation_off_new_entities_wait(self, town):
        town.set_profile(PROFILE, entity_moderation=False)
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        town.run()
        assert [c[1] for c in town.api.calls if c[0] == "POST"] == ["events"]
        assert town.submitted_events()[0]["venue_name"] == "Lake Tahoe Arena"

    def test_a_held_entity_reads_as_held_not_failed(self, town):
        town.set_profile(PROFILE, entity_moderation=False)
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        town.run()
        [row] = town.conn.execute("SELECT ok, detail, error FROM agent_activity "
                                  "WHERE action = 'ship-venue'").fetchall()
        assert row["ok"] == 1 and row["error"] is None
        assert "held (entity-moderation-off)" in row["detail"]

    def test_with_the_entity_gate_the_venue_is_submitted_with_the_event(self, town):
        town.api.entity_status = "pending"
        town.set_profile(PROFILE, entity_moderation=True)
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        town.run()
        assert [c[1] for c in town.api.calls if c[0] == "POST"] == ["venues", "events"]


class TestTheGate:
    def test_a_published_create_opens_the_circuit_and_stops_all_submissions(self, town):
        town.api.event_status = "published"
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night"), concert("Blues Night", days=8)])
        town.run()
        assert len(town.submitted_events()) == 1
        from engine import autonomy
        assert autonomy.circuit(town.conn)["reason"] == "published-without-review"
        assert any("STOPPED" in t for t in town.telegrams)
        town.drain(2)
        assert len(town.submitted_events()) == 1

    def test_the_pending_cap_is_respected(self, town):
        town.set_profile(PROFILE, max_pending=3)   # the venue + two events
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert(f"Show {i}", days=3 + i) for i in range(4)])
        town.run()
        town.drain(2)
        assert [c[1] for c in town.api.calls if c[0] == "POST"].count("venues") == 1
        assert len(town.submitted_events()) == 2
        held = town.conn.execute("SELECT COUNT(*) FROM events WHERE state = 'entities-enriched'"
                                 ).fetchone()[0]
        assert held == 2   # still queued, no attempts spent

    def test_the_pause_flag_stops_submissions(self, town):
        town.set_profile(PROFILE, paused=True)
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        town.run()
        assert town.submitted_events() == []
        town.set_profile(PROFILE)
        town.drain()
        assert town.submitted_titles() == ["Jazz Night"]

    def test_an_event_townloops_rejected_is_never_resubmitted(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night", source_url="https://arena.example/jazz")])
        town.run()
        [sent] = town.submitted_events()
        town.api.reject("events", sent["slug"])
        # The same event turns up again from a second seed.
        other = "https://visit.example/calendar"
        town.seed("Visitor Bureau", other, kind="tourism")
        town.listing(other, [concert("Jazz Night", source_url="https://arena.example/jazz",
                                     date=soon(7, hour=19))])
        town.conn.execute("UPDATE events SET status = 'superseded'")   # forget local dedup
        town.conn.commit()
        town.run()
        assert len(town.submitted_events()) == 1
        assert "rejected-on-townloops" in town.reasons().values()
