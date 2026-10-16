"""Autonomous mode, sources (issue #1): the agent approves proposed seeds
from a dry run, classifies them, replaces a dead seed whose calendar moved
(or retires it when it can't be found), merges the best metadata of every
copy of an event, and settles stuck events itself. Driven through the seed
review, event-find and the drain against the fake API."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from engine import autonomy
from tests.autonomy_town import PROFILE, Town, page_text, soon

ARENA = "https://arena.example/events"


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


def concert(title="Jazz Night", days=7, **kw):
    return {"title": title, "date": soon(days), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live music.",
            "categories": ["music"], "tags": [], "source_url": None, **kw}


class TestProposedSeeds:
    def test_a_seed_whose_dry_run_yields_upcoming_in_town_events_is_approved(self, town):
        town.seed("Arena", ARENA, status="proposed")
        town.listing(ARENA, [concert("Jazz Night")])
        town.seed_review()
        assert town.seed_row("Arena")["status"] == "approved"
        town.run()
        assert town.submitted_titles() == ["Jazz Night"]

    @pytest.mark.parametrize("page,events,reason", [
        ("Just a moment... Checking your browser before accessing.", [], "trial-challenge"),
        ("404 — Page not found", [], "trial-not-found"),
        (None, [concert("Old Show", days=-5)], "trial-no-usable-events"),
        (None, [], "trial-empty-listing"),
    ])
    def test_error_challenge_and_empty_pages_fail_the_trial(self, town, page, events, reason):
        town.seed("Arena", ARENA, status="proposed")
        town.listing(ARENA, events)
        if page:
            town.pages[ARENA] = page_text(page)
        town.seed_review()
        assert town.seed_row("Arena")["status"] == "rejected"
        [d] = town.decisions("seed")
        assert d["reason"] == reason

    @pytest.mark.parametrize("page,reason,named", [
        ("Request unsuccessful. Incapsula incident ID: 1234000560137281234-5678901234",
         "trial-challenge", "incapsula challenge"),
        ("# Page Not Found\n\nThe page you requested could not be found.", "trial-not-found",
         "platform 404 page"),
        ("no action", "trial-empty-page", "showare no action"),
        ("Loading...", "trial-empty-page", "near-empty body"),
    ])
    def test_the_trial_uses_the_one_blocked_page_detector(self, town, page, reason, named):
        town.seed("Arena", ARENA, status="proposed")
        town.listing(ARENA, [concert("Jazz Night")])
        town.pages[ARENA] = page                  # unpadded: the page as it came back
        town.seed_review()
        assert town.seed_row("Arena")["status"] == "rejected"
        [d] = town.decisions("seed")
        assert d["reason"] == reason
        assert d["evidence"]["page"] == named

    def test_seeds_with_a_feed_are_tried_first(self, town):
        town.seed("Plain", "https://plain.example/events", status="proposed")
        town.seed("Fed", "https://fed.example/events", status="proposed",
                  feed_type="ical", feed_url="https://fed.example/cal.ics")
        town.listing("https://fed.example/events", [concert("Jazz Night")])
        town.listing("https://plain.example/events", [concert("Blues Night")])
        town.seed_review("--trials", "1")
        assert town.seed_row("Fed")["status"] == "approved"
        assert town.seed_row("Plain")["status"] == "proposed"


class TestDeadSeeds:
    def _go_quiet(self, town, runs=4):
        town.listing(ARENA, [])
        for _ in range(runs):
            town.find()

    def test_a_dead_seed_whose_calendar_moved_is_replaced_not_retired(self, town):
        town.seed("Arena", ARENA)
        self._go_quiet(town)
        moved = "https://arena.example/calendar"
        town.redirects[ARENA] = moved
        town.listing(moved, [concert("Jazz Night")])
        town.seed_review()
        seed = town.seed_row("Arena")
        assert (seed["status"], seed["url"], seed["previous_url"]) == ("approved", moved, ARENA)
        town.run()
        assert town.submitted_titles() == ["Jazz Night"]

    def test_a_same_site_search_can_find_the_moved_calendar(self, town):
        town.seed("Arena", ARENA)
        self._go_quiet(town)
        moved = "https://arena.example/whats-on"
        town.search["site:arena.example events calendar"] = [
            {"url": "https://elsewhere.example/events"}, {"url": moved}]
        town.listing(moved, [concert("Jazz Night")])
        town.seed_review()
        assert town.seed_row("Arena")["url"] == moved

    def test_a_dead_seed_with_no_calendar_to_find_is_retired_reversibly(self, town):
        town.seed("Arena", ARENA)
        self._go_quiet(town)
        town.seed_review()
        seed = town.seed_row("Arena")
        assert seed["status"] == "retired"
        [d] = [d for d in town.decisions("seed") if d["verdict"] == "retire"]
        assert d["reason"] == "dead-source"

        from tests.autonomy_town import SKILLS, load
        mod = load(SKILLS / "event-seed-discovery" / "discover.py", "seed_restore")
        town.mp.setattr(mod, "_DB_PATH", town.state / "townloops.db")
        assert mod.main(["restore", "--seed-id", str(seed["seed_id"])]) == 0
        assert town.seed_row("Arena")["status"] == "approved"

    def test_a_seed_is_not_judged_dead_before_enough_runs(self, town):
        town.seed("Arena", ARENA)
        self._go_quiet(town, runs=2)
        town.seed_review()
        assert town.seed_row("Arena")["status"] == "approved"


class TestRoles:
    def test_a_calendar_of_other_sites_events_is_a_syndicator(self, town):
        town.seed("Visitor Bureau", "https://visit.example/calendar", kind="other")
        town.listing("https://visit.example/calendar", [
            concert(f"Show {i}", days=3 + i, source_url=f"https://venue{i}.example/e/{i}")
            for i in range(5)])
        town.run()
        town.seed_review()
        assert town.seed_row("Visitor Bureau")["source_role"] == "syndicator"
        [d] = town.decisions("seed-role")
        assert d["reason"] == "links-off-site"


class TestCopies:
    def test_two_seeds_listing_one_event_submit_it_once_with_both_copies_metadata(self, town):
        town.seed("Arena", ARENA, kind="venue")
        visit = "https://visit.example/calendar"
        town.seed("Visitor Bureau", visit, kind="tourism")
        town.listing(ARENA, [concert("Jazz Night", description="Live jazz in the lounge.")])
        town.listing(visit, [concert("Jazz Night", venue=None, description=None,
                                     end_at=soon(7, hour=22),
                                     source_url="https://arena.example/jazz")])
        town.run()
        [sent] = town.submitted_events()
        assert sent["description"] == "Live jazz in the lounge."     # the organiser's copy
        assert sent["source_url"] == "https://arena.example/jazz"    # lent by the other copy
        assert sent["end_at"]


class TestStuckEvents:
    def test_a_stuck_event_is_retried_then_shipped_with_what_it_has(self, town, monkeypatch):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        paginate = town.api.paginate

        def venues_down(resource, **kw):
            if resource == "venues":
                raise RuntimeError("[503] venues unavailable")
            return paginate(resource, **kw)
        monkeypatch.setattr(town.api, "paginate", venues_down)
        town.run()
        assert town.conn.execute("SELECT state FROM events").fetchone()[0] == "stuck-detail-read"
        assert town.submitted_events() == []

        from engine import orchestrator
        mode = autonomy.settings()
        later = datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)
        assert orchestrator.resolve_stuck(town.conn, mode, now=later) == {"retry": 1}
        town.drain_all()
        assert town.submitted_events() == []           # still failing, stuck again
        later += timedelta(hours=2, minutes=1)
        assert orchestrator.resolve_stuck(town.conn, mode, now=later) == {"retry": 1}
        town.drain_all()   # fails out again; retries exhausted → ships without a venue
        [sent] = town.submitted_events()
        assert sent["title"] == "Jazz Night" and "venue_id" not in sent
        assert [d["reason"] for d in town.decisions("stuck")][0] == "retries-exhausted"

    def test_a_stuck_event_without_a_start_is_dropped(self):
        s = autonomy.Settings(mode="autonomous")
        d = autonomy.resolve_stuck({"state": "stuck-entities-enriched", "title": "X",
                                    "extra_json": '{"autonomy": {"retries": 2}}'}, s)
        assert (d.verdict, d.reason) == ("drop", "missing-required-fields")

    def test_supervised_mode_leaves_stuck_events_to_the_operator(self):
        d = autonomy.resolve_stuck({"state": "stuck-detail-read"}, autonomy.Settings())
        assert d.verdict == autonomy.AWAIT
