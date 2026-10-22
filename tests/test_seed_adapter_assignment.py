"""Assigning a seed adapter (issue #29): `set-adapter` dry-runs the adapter
and saves it only when the trial returns events; the autonomous `review`
assigns the adapter a seed's probed `feed_type` names, supervised `review`
only proposes it. Every assignment is a recorded decision. Driven in a
whole town (tests/autonomy_town.py); the adapter is faked, no network."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from engine import seed_adapters
from tests.autonomy_town import PROFILE, Town
from tests.test_seed_maintenance import ARENA, concert, seeds_cli, why

FEED = "https://arena.example/wp-json/tribe/events/v1/events"
SUPERVISED = {**PROFILE, "operating_mode": "supervised"}


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


class Feed:
    """The `tribe` adapter answering canned events (or failing); its reads
    are counted."""

    def __init__(self, town: Town, events=None, error=None):
        self.events = [concert("Jazz Night"), concert("Blues Night", days=9)] \
            if events is None else events
        self.error = error
        self.reads: list[dict] = []
        town.mp.setitem(seed_adapters.ADAPTERS, "tribe",
                        replace(seed_adapters.ADAPTERS["tribe"], read=self))

    def __call__(self, seed, config, get_json):
        self.reads.append({**seed, "config": config})
        if self.error:
            raise seed_adapters.AdapterError(self.error)
        return list(self.events)


def adapter_decisions(town: Town) -> list[dict]:
    return town.decisions("seed-adapter")


def tribe_seed(town: Town, name="Arena", url=ARENA, **cols) -> int:
    return town.seed(name, url, feed_type="tribe", feed_url=FEED,
                     feed_probed_at="2026-01-01T00:00:00+00:00", **cols)


def backdate(town: Town, days: int) -> None:
    """Every adapter decision happened `days` ago."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    town.conn.execute("UPDATE agent_activity SET ts = ? WHERE action LIKE 'seed-adapter:%'", (ts,))
    town.conn.commit()


def reprobe(town: Town, seed_id: int) -> None:
    town.conn.execute("UPDATE event_seeds SET feed_probed_at = ? WHERE seed_id = ?",
                      ("2999-01-01T00:00:00+00:00", seed_id))
    town.conn.commit()


class TestSetAdapter:
    def test_an_unknown_adapter_is_refused(self, town):
        sid = town.seed("Arena", ARENA)
        rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "ticketleap")
        assert rc == 1 and "unknown adapter 'ticketleap'" in out["error"]
        assert town.seed_row("Arena")["adapter"] is None
        assert adapter_decisions(town) == []

    @pytest.mark.parametrize("config,named", [
        ('{"days_ahaed": 90}', "days_ahaed"),
        ('{"days_ahead": "soon"}', "days_ahead"),
        ("[1]", "JSON object"),
        ("{not json", "not JSON"),
    ])
    def test_invalid_config_is_refused_by_name(self, town, config, named):
        feed = Feed(town)
        sid = town.seed("Arena", ARENA)
        rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "tribe",
                            "--config", config)
        assert rc == 1 and named in out["error"]
        assert feed.reads == []          # refused before any trial
        seed = town.seed_row("Arena")
        assert (seed["adapter"], seed["adapter_config"]) == (None, None)

    @pytest.mark.parametrize("error,events,why_text", [
        ("page 1: http 503", None, "http 503"),
        (None, [], "no usable events"),
        (None, [{**concert(), "title": None}, {**concert(), "date": None}], "no usable events"),
    ])
    def test_a_failed_or_empty_trial_leaves_the_seed_unchanged(self, town, error, events,
                                                               why_text):
        Feed(town, events=events, error=error)
        sid = town.seed("Arena", ARENA)
        rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "tribe")
        assert rc == 1 and why_text in out["error"] and "unchanged" in out["error"]
        assert town.seed_row("Arena")["adapter"] is None
        assert adapter_decisions(town) == []

    def test_a_passed_trial_saves_the_adapter_and_why_shows_the_event_count(self, town):
        feed = Feed(town)
        sid = town.seed("Arena", ARENA)
        rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "tribe",
                            "--config", '{"days_ahead": 90}')
        assert rc == 0, out
        assert (out["adapter"], out["trial_events"]) == ("tribe", 2)
        assert feed.reads[0]["config"] == {"days_ahead": 90}
        seed = town.seed_row("Arena")
        assert (seed["adapter"], seed["adapter_config"]) == ("tribe", '{"days_ahead": 90}')
        [d] = why(town, str(sid))
        assert (d["subject"], d["verdict"], d["reason"]) == ("seed-adapter", "assign",
                                                             "trial-passed")
        assert d["evidence"]["trial_events"] == 2
        assert (d["evidence"]["from"], d["evidence"]["adapter"]) == ("generic", "tribe")
        town.run()
        assert town.submitted_titles() == ["Blues Night", "Jazz Night"]

    def test_the_trial_counts_only_events_event_find_would_keep(self, town):
        Feed(town, events=[concert("Jazz Night"), {**concert(), "title": None}])
        sid = town.seed("Arena", ARENA)
        rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "tribe")
        assert (rc, out["trial_events"]) == (0, 1)
        assert why(town, str(sid))[0]["evidence"]["trial_events"] == 1

    @pytest.mark.parametrize("status", ["rejected", "retired"])
    def test_a_rejected_or_retired_seed_is_refused_pointing_at_restore(self, town, status):
        feed = Feed(town)
        sid = town.seed("Arena", ARENA, status=status)
        for adapter in ("tribe", "generic"):
            rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", adapter)
            assert rc == 1 and status in out["error"] and "restore" in out["error"]
        assert feed.reads == [] and adapter_decisions(town) == []

    def test_generic_on_a_generic_seed_changes_nothing_and_records_nothing(self, town):
        sid = town.seed("Arena", ARENA)
        rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "generic")
        assert (rc, out["changed"]) == (0, False), out
        assert adapter_decisions(town) == []

    def test_a_failed_release_leaves_the_seed_held_and_generic(self, town):
        """The adapter write and the release are one transaction."""
        Feed(town)
        sid = town.seed("Arena", ARENA)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "awaiting-adapter")
        from engine import autonomy
        record = autonomy.record

        def failing(conn, subject, target, d):
            if d.verdict == "release":     # after release's status write
                raise sqlite3.OperationalError("disk I/O error")
            record(conn, subject, target, d)
        town.mp.setattr(autonomy, "record", failing)
        with pytest.raises(sqlite3.OperationalError):
            seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "tribe")
        seed = town.seed_row("Arena")
        assert (seed["status"], seed["hold_reason"], seed["adapter"]) == \
            ("held", "awaiting-adapter", None)
        assert adapter_decisions(town) == []

    def test_generic_reverts_without_a_trial_and_is_recorded(self, town):
        feed = Feed(town, error="would fail")
        sid = town.seed("Arena", ARENA, adapter="tribe", adapter_config='{"days_ahead": 90}')
        rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "generic")
        assert rc == 0, out
        assert feed.reads == []
        seed = town.seed_row("Arena")
        assert (seed["adapter"], seed["adapter_config"]) == (None, None)
        [d] = why(town, str(sid))
        assert (d["verdict"], d["evidence"]) == ("revert", {"from": "tribe", "to": "generic"})

    def test_a_seed_held_awaiting_an_adapter_is_released_after_a_good_trial(self, town):
        Feed(town)
        sid = town.seed("Arena", ARENA)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "awaiting-adapter")
        rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "tribe")
        assert (rc, out["status"]) == (0, "proposed"), out
        assert [d["verdict"] for d in why(town, str(sid))[:2]] == ["release", "assign"]
        # The release's trial reads the seed through its adapter (the page itself
        # has nothing on it), then event-find does.
        town.seed_review()
        assert town.seed_row("Arena")["status"] == "approved"
        town.run()
        assert town.submitted_titles() == ["Blues Night", "Jazz Night"]

    def test_a_failed_trial_keeps_an_awaiting_adapter_seed_held(self, town):
        Feed(town, error="http 404")
        sid = town.seed("Arena", ARENA)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "awaiting-adapter")
        rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "tribe")
        assert rc == 1
        assert town.seed_row("Arena")["status"] == "held"

    def test_a_seed_held_manual_only_stays_held(self, town):
        Feed(town)
        sid = town.seed("Arena", ARENA)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "manual-only")
        rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "tribe")
        assert (rc, out["status"]) == (0, "held"), out
        assert town.seed_row("Arena")["adapter"] == "tribe"

    def test_supervised_release_returns_an_approved_seed_to_service(self, town):
        town.set_profile(SUPERVISED)
        Feed(town)
        sid = town.seed("Arena", ARENA)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "awaiting-adapter")
        rc, out = seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "tribe")
        assert (rc, out["status"]) == (0, "approved"), out


class TestAutonomousReview:
    def test_a_seed_whose_probed_feed_an_adapter_reads_is_assigned_it(self, town):
        Feed(town)
        sid = tribe_seed(town)
        out = town.seed_review()
        assert (out["adapters_tried"], out["adapters_assigned"]) == (1, 1)
        seed = town.seed_row("Arena")
        assert (seed["adapter"], seed["adapter_config"], seed["status"]) == \
            ("tribe", None, "approved")
        d = next(d for d in why(town, str(sid)) if d["subject"] == "seed-adapter")
        assert (d["verdict"], d["reason"]) == ("assign", "feed-match")
        assert (d["evidence"]["trial_events"], d["evidence"]["feed_type"]) == (2, "tribe")

    def test_a_held_awaiting_adapter_seed_is_assigned_released_and_trialed(self, town):
        """The one held seed review touches: assigning the adapter it waits
        for, through the audited release — then it is trialed like any
        released seed (through the adapter: its page lists nothing)."""
        Feed(town)
        sid = tribe_seed(town)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "awaiting-adapter")
        town.seed_review()
        seed = town.seed_row("Arena")
        assert (seed["adapter"], seed["status"], seed["hold_reason"]) == \
            ("tribe", "approved", None)
        assert [(d["subject"], d["verdict"]) for d in why(town, str(sid))[:3]] == [
            ("seed", "approve"), ("seed", "release"), ("seed-adapter", "assign")]

    def test_a_held_manual_only_seed_is_left_alone(self, town):
        feed = Feed(town)
        sid = tribe_seed(town)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "manual-only")
        town.seed_review()
        assert feed.reads == []
        assert town.seed_row("Arena")["adapter"] is None

    def test_a_feed_no_adapter_reads_is_not_tried(self, town):
        feed = Feed(town)
        town.seed("Arena", ARENA, feed_type="ical", feed_url="https://arena.example/cal.ics")
        assert town.seed_review()["adapters_tried"] == 0
        assert feed.reads == []

    def test_a_failed_trial_is_tried_again_after_the_backoff(self, town):
        from engine import seeds
        feed = Feed(town, error="page 1: http 503")
        tribe_seed(town)
        town.seed_review()
        assert town.seed_row("Arena")["adapter"] is None
        [d] = adapter_decisions(town)
        assert (d["verdict"], d["reason"]) == ("keep-generic", "adapter-trial-failed")
        assert "http 503" in d["evidence"]["error"]
        town.seed_review()
        assert len(feed.reads) == 1                       # not every day
        backdate(town, seeds.ADAPTER_RETRY_DAYS + 1)
        feed.error = None
        town.seed_review()
        assert town.seed_row("Arena")["adapter"] == "tribe"

    def test_a_failed_trial_is_tried_again_after_a_new_probe(self, town):
        feed = Feed(town, error="page 1: http 503")
        sid = tribe_seed(town)
        town.seed_review()
        reprobe(town, sid)
        feed.error = None
        town.seed_review()
        assert town.seed_row("Arena")["adapter"] == "tribe"

    def test_an_empty_trial_keeps_the_seed_generic_until_its_feed_is_probed_again(self, town):
        from engine import seeds
        feed = Feed(town, events=[])
        sid = tribe_seed(town)
        town.seed_review()
        assert town.seed_row("Arena")["adapter"] is None
        assert adapter_decisions(town)[0]["reason"] == "adapter-trial-empty"
        backdate(town, seeds.ADAPTER_RETRY_DAYS + 1)
        town.seed_review()
        assert len(feed.reads) == 1                       # no backoff retry for an empty feed
        reprobe(town, sid)
        feed.events = [concert()]
        town.seed_review()
        assert town.seed_row("Arena")["adapter"] == "tribe"

    def test_a_supervised_proposal_is_assigned_once_the_town_is_autonomous(self, town):
        town.set_profile(SUPERVISED)
        Feed(town)
        tribe_seed(town)
        town.seed_review()
        assert town.seed_row("Arena")["adapter"] is None
        town.set_profile(PROFILE)
        town.seed_review()
        assert town.seed_row("Arena")["adapter"] == "tribe"

    def test_a_seed_reverted_to_generic_is_not_assigned_again(self, town):
        feed = Feed(town)
        sid = tribe_seed(town, adapter="tribe")
        seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "generic")
        reprobe(town, sid)
        town.seed_review()
        assert feed.reads == []
        assert town.seed_row("Arena")["adapter"] is None

    def test_rejected_and_retired_seeds_are_not_tried(self, town):
        feed = Feed(town)
        tribe_seed(town, "Gone", "https://gone.example/events", status="retired")
        tribe_seed(town, "No", "https://no.example/events", status="rejected")
        town.seed_review()
        assert feed.reads == []

    def test_a_run_tries_at_most_adapters_seeds(self, town):
        feed = Feed(town)
        for i in range(3):
            tribe_seed(town, f"S{i}", f"https://s{i}.example/events")
        assert town.seed_review("--adapters", "2")["adapters_tried"] == 2
        assert len(feed.reads) == 2

    def test_the_assignment_is_in_the_digest(self, town):
        Feed(town)
        tribe_seed(town)
        town.seed_review()
        out, md, tg = town.digest()
        assert "seed Arena now read by the tribe adapter (2 events in its trial)" in md


class TestSupervisedReview:
    def test_it_only_proposes_the_adapter(self, town):
        town.set_profile(SUPERVISED)
        Feed(town)
        sid = tribe_seed(town)
        out = town.seed_review()
        assert (out["adapters_tried"], out["adapters_proposed"], out["adapters_assigned"]) == \
            (1, 1, 0)
        assert town.seed_row("Arena")["adapter"] is None
        [d] = why(town, str(sid))
        assert (d["verdict"], d["reason"]) == ("await-operator", "supervised")
        assert (d["evidence"]["adapter"], d["evidence"]["trial_events"]) == ("tribe", 2)

    def test_it_does_not_trial_approve_or_retire_seeds(self, town):
        town.set_profile(SUPERVISED)
        town.seed("New", ARENA, status="proposed")
        town.listing(ARENA, [concert()])
        town.seed_review()
        assert town.seed_row("New")["status"] == "proposed"
        assert town.decisions("seed") == []

    def test_a_proposal_is_in_the_digest_until_the_operator_acts(self, town):
        town.set_profile(SUPERVISED)
        feed = Feed(town)
        sid = tribe_seed(town)
        town.seed_review()
        town.seed_review()
        assert len(feed.reads) == 1                       # proposed once
        out, md, tg = town.digest()
        assert out["adapter_proposals"] == [{
            "seed_id": sid, "name": "Arena", "status": "approved", "adapter": "tribe",
            "feed_type": "tribe", "trial_events": 2,
            "command": f"event-seed-discovery set-adapter --seed-id {sid} --adapter tribe"}]
        assert "## Adapter proposals" in md
        assert f"Arena (#{sid}) — tribe feed, 2 events in a dry run" in md
        assert "adapter proposals: Arena (tribe)" in tg
        seeds_cli(town, "set-adapter", "--seed-id", str(sid), "--adapter", "tribe")
        out, md, tg = town.digest()
        assert out["adapter_proposals"] == [] and "Adapter proposals" not in md

    def test_a_held_awaiting_adapter_seed_is_proposed_too(self, town):
        town.set_profile(SUPERVISED)
        Feed(town)
        sid = tribe_seed(town)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "awaiting-adapter")
        town.seed_review()
        seed = town.seed_row("Arena")
        assert (seed["status"], seed["adapter"]) == ("held", None)
        assert town.digest()[0]["adapter_proposals"][0]["status"] == "held"
