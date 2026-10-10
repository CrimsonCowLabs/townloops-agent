"""Autonomous mode, the moderation feedback loop (issue #1): what the
TownLoops moderator does with each submission — publish, reject, merge —
comes back through the hourly poll and steers what the agent submits next.
Driven through event-find, the drain, moderation-feedback and the seed
review against the fake API (tests/autonomy_town.py)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from engine.event_extract import upsert_event, utc_times
from tests.autonomy_town import PROFILE, Town, soon

ARENA = "https://arena.example/events"


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


def concert(title="Jazz Night", days=7, **kw):
    return {"title": title, "date": soon(days), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live music.",
            "categories": ["music"], "tags": [], "source_url": None, **kw}


def slugs(town):
    return [p["slug"] for p in town.submitted_events()]


class TestSeedTrust:
    def test_rejections_lower_trust_and_repeated_rejections_retire_the_seed(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert(f"Show {i}", days=3 + i) for i in range(5)])
        town.run()
        assert len(town.submitted_events()) == 5
        first, *rest = slugs(town)

        town.api.reject("events", first)
        town.poll()
        assert town.seed_row("Arena")["trust_score"] < 0.5

        for slug in rest:
            town.api.reject("events", slug)
        town.poll()
        town.seed_review()
        seed = town.seed_row("Arena")
        assert seed["status"] == "retired"
        [d] = [d for d in town.decisions("seed") if d["verdict"] == "retire"]
        assert d["reason"] == "low-trust" and d["evidence"]["rejected"] == 5
        assert d["evidence"]["trust"] < d["evidence"]["threshold"]

        town.listing(ARENA, [concert("Show 9", days=12)])
        town.run()
        assert len(town.submitted_events()) == 5          # a retired seed isn't scraped

    def test_published_events_raise_trust(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        town.run()
        town.api.approve("events", slugs(town)[0])
        town.poll()
        assert town.seed_row("Arena")["trust_score"] > 0.5
        [d] = town.decisions("submission")
        assert (d["verdict"], d["evidence"]["seed"]) == ("published", "Arena")


class TestMerges:
    def test_a_merged_submission_is_repointed_to_the_surviving_event(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        start, _ = utc_times(soon(7), None)
        town.api.events["jazz-at-the-arena"] = {
            "id": 99, "slug": "jazz-at-the-arena", "title": "Jazz Night at the Arena",
            "start_at": start, "status": "published", "source_url": None, "venue_id": None,
            "series_slug": None, "tags": [], "category_slugs": []}
        town.run()
        [mine] = slugs(town)
        town.api.merge("events", mine, into="jazz-at-the-arena")
        town.poll()
        row = town.conn.execute("SELECT townloops_slug FROM events WHERE title = 'Jazz Night'"
                                ).fetchone()
        assert row[0] == "jazz-at-the-arena"
        [d] = town.decisions("submission")
        assert d["verdict"] == "merged" and d["evidence"]["surviving_slug"] == "jazz-at-the-arena"

    def test_a_date_change_to_a_published_event_is_submitted_for_merge_not_patched(self, town):
        old, new = soon(7), soon(9)
        old_utc, _ = utc_times(old, None)
        town.api.events["jazz-live"] = {
            "id": 50, "slug": "jazz-live", "title": "Jazz Night", "start_at": old_utc,
            "status": "published", "source_url": "https://arena.example/jazz",
            "venue_id": None, "series_slug": None, "tags": [], "category_slugs": []}
        town.seed("Arena", ARENA)
        moved = concert("Jazz Night", date=new, source_url="https://arena.example/jazz")
        town.listing(ARENA, [moved])
        # event-reconcile staged the source's new date as a change to the live event.
        upsert_event(town.conn, "Arena", moved)
        town.conn.execute("UPDATE events SET extra_json = ?",
                          (json.dumps({"reconcile": {"kind": "changed", "live_slug": "jazz-live",
                                                     "changes": {"start_at": [old, new]}}}),))
        town.conn.commit()
        town.run()

        assert town.api.patches() == []
        [sent] = town.submitted_events()
        assert sent["source_url"] == "https://arena.example/jazz"
        assert sent["start_at"] == utc_times(new, None)[0]
        assert town.api.events["jazz-live"]["start_at"] == old_utc   # untouched until merged

        town.api.merge("events", sent["slug"], into="jazz-live")
        town.poll()
        assert town.api.events["jazz-live"]["start_at"] == utc_times(new, None)[0]
        row = town.conn.execute("SELECT townloops_slug FROM events WHERE status = 'active'"
                                ).fetchone()
        assert row[0] == "jazz-live"


JAZZ_URL = "https://arena.example/jazz"
ARENA_VENUE = {"id": 6, "slug": "lake-tahoe-arena", "name": "Lake Tahoe Arena"}
OLD_HALL = {"id": 5, "slug": "old-hall", "name": "Old Hall"}


def stage_change(town, changes: dict, live: dict, **event) -> dict:
    """A published event on TownLoops and event-reconcile's staged change to it.
    The source's venue is a live venue the agent already mirrors locally."""
    from engine.derive_helpers import mirror_live_entity
    town.api.venues.update({"lake-tahoe-arena": ARENA_VENUE, "old-hall": OLD_HALL})
    mirror_live_entity(town.conn, "venue", ARENA_VENUE)
    start, _ = utc_times(soon(7), None)
    town.api.events["jazz-live"] = {
        "id": 50, "slug": "jazz-live", "title": "Jazz Night", "start_at": start,
        "status": "published", "source_url": JAZZ_URL, "venue_id": None, "venue_slug": None,
        "series_slug": None, "tags": [], "category_slugs": [], **live}
    town.seed("Arena", ARENA)
    ev = concert("Jazz Night", source_url=JAZZ_URL, **event)
    town.listing(ARENA, [ev])
    upsert_event(town.conn, "Arena", ev)
    town.conn.execute("UPDATE events SET extra_json = ?",
                      (json.dumps({"reconcile": {"kind": "changed", "live_slug": "jazz-live",
                                                 "changes": changes}}),))
    town.conn.commit()
    return town.api.events["jazz-live"]


def change_decisions(town) -> dict[str, dict]:
    return {d["verdict"]: d for d in town.decisions("change")}


class TestChanges:
    """Every reconcile-staged change ends up somewhere: a change to a
    published value goes to the moderator as a pending submission for the
    merge flow, one that only fills a blank live field is PATCHed live."""

    def test_a_venue_change_is_submitted_and_a_venue_only_merge_reads_as_merged(self, town):
        stage_change(town, {"venue": {"live": "Old Hall", "live_slug": "old-hall",
                                      "source": "Lake Tahoe Arena"}},
                     {"venue_id": 5, "venue_slug": "old-hall"})
        town.run()
        assert town.api.patches() == []
        [sent] = town.submitted_events()
        assert sent["venue_id"] == 6 and sent["source_url"] == JAZZ_URL
        assert town.api.events["jazz-live"]["venue_id"] == 5     # untouched until merged

        town.api.merge("events", sent["slug"], into="jazz-live", fields=("venue_id", "venue_slug"))
        out = town.poll()
        assert (out["merged"], out["gone"]) == (1, 0)
        row = town.conn.execute("SELECT townloops_slug FROM events WHERE status = 'active'"
                                ).fetchone()
        assert row[0] == "jazz-live"

    def test_a_change_that_only_fills_a_blank_live_field_is_patched_not_submitted(self, town):
        stage_change(town, {"venue": {"live": None, "live_slug": None,
                                      "source": "Lake Tahoe Arena"}}, {})
        town.run()
        assert town.submitted_events() == []
        assert town.api.events["jazz-live"]["venue_id"] == 6
        d = change_decisions(town)["patch"]
        assert d["reason"] == "fill-blank-live" and d["evidence"]["fields"] == ["venue_id"]
        row = town.conn.execute("SELECT townloops_slug, state FROM events").fetchone()
        assert tuple(row) == ("jazz-live", "shipped")

    def test_other_changed_fields_are_submitted_too(self, town):
        stage_change(town, {"title": {"live": "Jazz Nite", "source": "Jazz Night"}},
                     {"title": "Jazz Nite"})
        town.run()
        assert town.api.patches() == []
        [sent] = town.submitted_events()
        assert sent["title"] == "Jazz Night"
        d = change_decisions(town)["submit"]
        assert d["evidence"]["changes"] == ["title"]

        town.api.merge("events", sent["slug"], into="jazz-live", fields=("title",))
        out = town.poll()
        assert (out["merged"], out["gone"]) == (1, 0)

    def test_a_date_change_submits_the_date_and_patches_live_blanks(self, town):
        new = soon(9)
        stage_change(town, {"start_at": {"live": "x", "source": utc_times(new, None)[0]}},
                     {}, date=new)
        town.conn.execute("UPDATE events SET price = 'Free', "
                          "accessibility_notes = 'Wheelchair accessible.'")
        town.conn.commit()
        town.run()
        [sent] = town.submitted_events()
        assert sent["start_at"] == utc_times(new, None)[0]
        live = town.api.events["jazz-live"]
        assert live["start_at"] != sent["start_at"]                  # published value: merge only
        assert live["tags"] == ["free"]                              # blanks: live directly
        assert live["accessibility_notes"] == "Wheelchair accessible."
        verdicts = change_decisions(town)
        assert verdicts["submit"]["evidence"]["changes"] == ["start_at"]
        assert "accessibility_notes" in verdicts["patch"]["evidence"]["fields"]

    def test_ticket_fills_that_rewrite_published_values_are_submitted_not_patched(self, town):
        # A ticket line atop a published description, or `free` dropped from
        # published tags, changes a published value: merge flow, never a PATCH.
        new = soon(9)
        stage_change(town, {"start_at": {"live": "x", "source": utc_times(new, None)[0]}},
                     {"description": "Live jazz.", "tags": ["free", "music"]}, date=new)
        town.conn.execute("UPDATE events SET price = '$20'")
        town.conn.commit()
        town.run()
        live = town.api.events["jazz-live"]
        assert (live["description"], live["tags"]) == ("Live jazz.", ["free", "music"])
        [sent] = town.submitted_events()
        assert sent["description"].startswith("Tickets: $20")
        assert "free" not in sent["tags"]
        assert set(change_decisions(town)["submit"]["evidence"]["fields"]) >= {
            "description", "tags", "start_at"}

    def test_a_deleted_venue_change_reads_as_gone_not_merged(self, town):
        # The date never changed, so the target "showing our start time" is no
        # evidence of a merge.
        stage_change(town, {"venue": {"live": "Old Hall", "live_slug": "old-hall",
                                      "source": "Lake Tahoe Arena"}},
                     {"venue_id": 5, "venue_slug": "old-hall"})
        town.run()
        [sent] = town.submitted_events()
        town.api.delete("events", sent["slug"])
        out = town.poll()
        assert (out["merged"], out["gone"]) == (0, 1)


class TestCircuitBreaker:
    def test_a_high_rejection_rate_stops_submissions_and_alerts(self, town):
        town.set_profile(PROFILE, circuit_breaker={"reject_rate": 0.5, "min_outcomes": 4,
                                                   "window": 10})
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert(f"Show {i}", days=3 + i) for i in range(4)])
        town.run()
        for slug in slugs(town)[:3]:
            town.api.reject("events", slug)
        town.api.approve("events", slugs(town)[3])
        out = town.poll()
        assert out["circuit_opened"]["rate"] == 0.75
        assert any("rejection-rate" in t for t in town.telegrams)

        town.listing(ARENA, [concert("Show 9", days=12)])
        town.run()
        assert len(town.submitted_events()) == 4
        # still collected and judged, waiting at ship
        assert town.conn.execute("SELECT state FROM events WHERE title = 'Show 9'"
                                 ).fetchone()[0] == "entities-enriched"


TRIVIA = {"id": 1, "slug": "trivia-night", "title": "Trivia Night"}


class TestSeries:
    def test_a_same_date_copy_of_a_series_occurrence_is_dropped(self, town):
        start, _ = utc_times(soon(7), None)
        town.api.series["trivia-night"] = {**TRIVIA, "is_active": True, "town": "slt",
                                           "cadence_text": None, "description": None}
        town.api.events["trivia-wk1"] = {
            "id": 7, "slug": "trivia-wk1", "title": "Trivia Night", "start_at": start,
            "status": "published", "series_slug": "trivia-night", "series_id": 1,
            "source_url": "https://bar.example/trivia", "venue_id": None, "tags": [],
            "category_slugs": []}
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Trivia Night", date=soon(7, hour=20))])
        town.run()
        assert town.submitted_events() == []
        assert town.reasons()["Trivia Night"] == "series-duplicate"

    def test_a_series_the_moderator_keeps_rejecting_stops_until_one_is_published(self, town):
        town.api.series["trivia-night"] = {**TRIVIA, "is_active": True, "town": "slt",
                                           "cadence_text": None, "description": None}
        town.set_profile(PROFILE, series_reject_streak=3)
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Trivia Night", days=d) for d in (3, 10, 17, 24)])
        town.run()
        assert len(town.submitted_events()) == 4
        *rejected, last = slugs(town)
        for slug in rejected:
            town.api.reject("events", slug)
        town.poll()

        town.listing(ARENA, [concert("Trivia Night", days=31)])
        town.run()
        assert len(town.submitted_events()) == 4
        assert "series-rejected-streak" in town.reasons().values()

        town.api.approve("events", last)
        town.poll()
        town.listing(ARENA, [concert("Trivia Night", days=38)])
        town.run()
        assert len(town.submitted_events()) == 5
