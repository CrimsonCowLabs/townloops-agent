"""event-ship and TownLoops Series (ADR 0007), through the skill's entry
point against the fake API: the title an occurrence ships under decides the
Series it auto-links to; a Series' occurrence on the same town-local date is
the event already live; the Series each shipped event joined is recorded
from TownLoops' answer; and blank Series-shared fields fill from siblings.
The agent never writes a Series."""

from __future__ import annotations

import json

from tests.fake_townloops import FakeAPI
from tests.test_live_lookup import (  # noqa: F401 — fixtures
    EV, _row, _stage, _use, conn, live_event, ship, snapshot,
)


TRIVIA = {"id": 11, "slug": "trivia-thursday", "title": "Trivia Thursday"}
JON = {"id": 12, "slug": "jon-sills", "title": "Jon Sills"}
LOFT, BAR_B, SKY = 69, 70, 71
VENUES = [{"id": LOFT, "slug": "the-loft", "name": "The Loft"},
          {"id": BAR_B, "slug": "bar-b", "name": "Bar B"},
          {"id": SKY, "slug": "divided-sky", "name": "Divided Sky"}]
SRC = "https://loft.example/events/trivia/2026-10-08"

# Thursday 2026-10-08 19:00 local is 2026-10-09T02:00Z; the staged event is
# a week on (Thursday 2026-10-15 19:00 local).
WEEK1, WEEK3 = "2026-10-09T02:00:00+00:00", "2026-10-16T02:00:00+00:00"


def trivia_occurrence(slug="trivia-wk1", start=WEEK1, venue_id=LOFT, **kw):
    return live_event(slug, start=start, source_url=SRC, title="Trivia Thursday",
                      venue_id=venue_id, series_slug="trivia-thursday", series_id=11, **kw)


def jon_gig(slug, start, venue_id=SKY):
    return live_event(slug, start=start, source_url=f"https://sky.example/{slug}",
                      title="Jon Sills", venue_id=venue_id, series_slug="jon-sills",
                      series_id=12)


def stage_at(ship, monkeypatch, conn, title, venue_slug="the-loft", venue_id=LOFT, **ev):
    """Stage an event at a live venue (a local mirror with its live id)."""
    return _stage(ship, monkeypatch, conn,
                  ev={**EV, "title": title, "date": "2026-10-15T19:00:00",
                      "source_url": "https://loft.example/events/trivia/2026-10-15", **ev},
                  venue={"townloops_slug": venue_slug, "source_kind": "live",
                         "name": next(v["name"] for v in VENUES if v["id"] == venue_id),
                         "extra_json": json.dumps({"live_id": venue_id})})


def link_performer(conn, uid, name):
    from engine.derive_helpers import insert_entity, link_event_to_entity
    insert_entity(conn, "performers", f"p-{name}", {"source": "derived",
                                                    "source_kind": "derived", "name": name})
    link_event_to_entity(conn, uid, "performer", f"p-{name}")


def event_creates(api):
    return [c[2] for c in api.calls if c[:2] == ("POST", "events")]


def activity(conn, action):
    return [r["detail"] for r in conn.execute(
        "SELECT detail FROM agent_activity WHERE worker = 'event-ship' AND action = ?",
        (action,))]


class TestJoiningTheRightSeries:
    def test_variant_at_the_series_venue_ships_under_the_series_title(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([trivia_occurrence()], venues=VENUES, series=[TRIVIA]))
        uid = stage_at(ship, monkeypatch, conn, "Trivia Thursday – Week 3")
        assert ship.main() == 0
        [created] = api.creates()
        assert created["title"] == "Trivia Thursday"
        assert api.events[created["slug"]]["series_slug"] == "trivia-thursday"
        row = _row(conn, uid)
        assert (row["series_slug"], row["series_id"]) == ("trivia-thursday", 11)
        assert row["title"] == "Trivia Thursday – Week 3"          # local title unchanged
        [detail] = activity(conn, "series-title")
        assert "series-title" in detail and "trivia-thursday" in detail

    def test_variant_at_another_venue_keeps_its_title(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([trivia_occurrence()], venues=VENUES, series=[TRIVIA]))
        uid = stage_at(ship, monkeypatch, conn, "Trivia Thursday – Week 3",
                       venue_slug="bar-b", venue_id=BAR_B,
                       source_url="https://barb.example/quiz")
        assert ship.main() == 0
        [created] = api.creates()
        assert created["title"] == "Trivia Thursday – Week 3"
        assert api.events[created["slug"]]["series_slug"] is None
        assert _row(conn, uid)["series_slug"] is None

    def test_performer_title_matching_a_series_elsewhere_names_the_venue(
            self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([jon_gig("jon-1", WEEK1), jon_gig("jon-2", WEEK3)],
                                        venues=VENUES, series=[JON]))
        uid = stage_at(ship, monkeypatch, conn, "Jon Sills",
                       source_url="https://loft.example/jon-sills")
        link_performer(conn, uid, "Jon Sills")
        assert ship.main() == 0
        [created] = event_creates(api)
        assert created["title"] == "Jon Sills at The Loft"
        assert api.events[created["slug"]]["series_slug"] is None

        # A re-ship finds the live row by slug and never touches its title.
        assert ship.main() == 0
        assert len(event_creates(api)) == 1
        assert api.events[created["slug"]]["title"] == "Jon Sills at The Loft"
        assert all(p.get("title") in (None, "Jon Sills at The Loft") for p in api.patches())

    def test_archived_series_groups_but_is_never_linked(self, ship, monkeypatch, conn):
        old = {**TRIVIA, "is_active": False}
        api = _use(monkeypatch, FakeAPI([trivia_occurrence()], venues=VENUES, series=[old]))
        uid = stage_at(ship, monkeypatch, conn, "Trivia Thursday – Week 3")
        assert ship.main() == 0
        [created] = api.creates()
        assert created["title"] == "Trivia Thursday – Week 3"
        assert _row(conn, uid)["series_slug"] is None

    def test_series_are_never_written(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([trivia_occurrence()], venues=VENUES, series=[TRIVIA]))
        stage_at(ship, monkeypatch, conn, "Trivia Thursday – Week 3")
        assert ship.main() == 0
        assert not [c for c in api.calls if c[0] in ("POST", "PATCH") and c[1] == "series"]


class TestSameDayInASeries:
    def test_second_source_copy_fills_the_live_occurrence(self, ship, monkeypatch, conn):
        live = trivia_occurrence("trivia-wk3", start="2026-10-16T02:30:00+00:00",
                                 description=None)
        api = _use(monkeypatch, FakeAPI([trivia_occurrence(), live], venues=VENUES,
                                        series=[TRIVIA]))
        uid = stage_at(ship, monkeypatch, conn, "Trivia Thursday!",
                       source_url="https://another-calendar.example/e/991",
                       description="Six rounds, prizes.")
        assert ship.main() == 0
        assert api.creates() == []
        assert api.events["trivia-wk3"]["description"] == "Six rounds, prizes."
        assert api.events["trivia-wk3"]["start_at"] == "2026-10-16T02:30:00+00:00"
        row = _row(conn, uid)
        assert (row["townloops_slug"], row["series_slug"]) == ("trivia-wk3", "trivia-thursday")

    def test_two_live_occurrences_on_the_date_park_the_event(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI(
            [trivia_occurrence("trivia-a", start=WEEK3),
             trivia_occurrence("trivia-b", start="2026-10-16T03:00:00+00:00")],
            venues=VENUES, series=[TRIVIA]))
        uid = stage_at(ship, monkeypatch, conn, "Trivia Thursday",
                       source_url="https://another-calendar.example/e/991")
        assert ship.main() != 0
        assert api.creates() == [] and api.patches() == []
        assert _row(conn, uid)["state"].startswith("stuck-")


class TestSeriesSharedEnrichment:
    def test_blank_description_fills_from_a_sibling_without_its_price(
            self, ship, monkeypatch, conn):
        sibling = trivia_occurrence(
            description="Tickets: $10 — https://loft.example/t\n\nSix rounds, prizes.",
            category_slugs=["nightlife"], tags=["free", "trivia"],
            accessibility_notes="Step-free entrance.", end_at="2026-10-09T04:00:00+00:00")
        api = _use(monkeypatch, FakeAPI([sibling], venues=VENUES, series=[TRIVIA]))
        stage_at(ship, monkeypatch, conn, "Trivia Thursday", description=None,
                 categories=[], tags=[])
        assert ship.main() == 0
        [created] = api.creates()
        new = api.events[created["slug"]]
        assert new["description"] == "Six rounds, prizes."
        assert new["category_slugs"] == ["nightlife"]
        assert new["tags"] == ["trivia"]
        assert new["accessibility_notes"] == "Step-free entrance."
        assert new["start_at"] == created["start_at"] and not new.get("end_at")
        assert api.events["trivia-wk1"]["description"].startswith("Tickets: $10")

    def test_venue_is_not_copied_across_a_multi_venue_series(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([trivia_occurrence(), trivia_occurrence(
            "trivia-wk2", start="2026-10-23T02:00:00+00:00", venue_id=BAR_B)],
            venues=VENUES, series=[TRIVIA]))
        uid = _stage(ship, monkeypatch, conn,
                     ev={**EV, "title": "Trivia Thursday", "date": "2026-10-15T19:00:00",
                         "source_url": "https://another-calendar.example/e/5"})
        assert ship.main() == 0
        [created] = api.creates()
        assert api.events[created["slug"]]["venue_id"] is None
        assert _row(conn, uid)["series_slug"] == "trivia-thursday"

    def test_venue_is_copied_for_a_single_venue_series(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([trivia_occurrence()], venues=VENUES, series=[TRIVIA]))
        _stage(ship, monkeypatch, conn,
               ev={**EV, "title": "Trivia Thursday", "date": "2026-10-15T19:00:00",
                   "source_url": "https://another-calendar.example/e/5"})
        assert ship.main() == 0
        [created] = api.creates()
        assert api.events[created["slug"]]["venue_id"] == LOFT

