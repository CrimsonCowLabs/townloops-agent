"""Event images and typed fields reach TownLoops (#64, stories 44-46, 51).

A whole town (tests/autonomy_town.py) finds an event, reads its own page and
submits it: the picture its page, feed or listing card offers — with alt text
copied from there, never made up — goes out as `image_url` + `image_alt`, and
the price, all-day-ness and age the source states go out typed. An event
already live only has its blanks filled: a picture it has is never replaced."""

from __future__ import annotations

import json
import sqlite3

import pytest

from engine.event_extract import upsert_event, utc_times
from tests.autonomy_town import PROFILE, Town, soon
from tests.fake_townloops import FakeAPI
from tests.test_autonomy_feedback import JAZZ_URL, stage_change
from tests.test_live_lookup import (  # noqa: F401 — fixtures
    _row, _stage, _use, conn, live_event, ship, snapshot,
)

ARENA = "https://arena.example/events"
PAGE = "https://arena.example/events/jazz-night"
TRIO = "https://cdn.arena.example/jazz-trio.jpg"
TRIO_ALT = "The trio on the lounge stage under blue lights"
OG = (f'<html><head><meta property="og:image" content="{TRIO}">'
      f'<meta property="og:image:alt" content="{TRIO_ALT}"></head></html>')


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


def concert(title="Jazz Night", days=7, **kw):
    return {"title": title, "date": soon(days), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live jazz.",
            "categories": ["music"], "tags": [], "source_url": PAGE, **kw}


def _event(town):
    return dict(town.conn.execute("SELECT * FROM events").fetchone())


class TestFromTheEventsOwnPage:
    def test_its_page_picture_price_and_age_are_submitted(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert()])
        town.page_html[PAGE] = OG
        town.pages[PAGE] = ("# Jazz Night\n\nTickets $15 at the door. This is a 21+ show.\n\n"
                            + "Live jazz in the lounge. " * 10)
        town.details[PAGE] = {"price_quote": "Tickets $15 at the door.",
                              "age_quote": "This is a 21+ show."}
        town.run()

        [sent] = town.submitted_events()
        assert (sent["image_url"], sent["image_alt"]) == (TRIO, TRIO_ALT)
        assert (sent["price_kind"], sent["price_min_cents"]) == ("paid", 1500)
        assert "price_max_cents" not in sent
        assert sent["age_restriction"] == "adults_only"
        assert sent["description"].startswith("Tickets: $15")      # the line stays
        assert "is_all_day" not in sent                             # a timed event
        live = town.api.events[town.api.posted_slug("events", sent)]
        assert live["image_path"] and live["image_alt"] == TRIO_ALT

    def test_an_age_quote_that_is_not_on_the_page_is_dropped(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert()])
        town.details[PAGE] = {"age_quote": "21 and over only."}
        town.run()
        [sent] = town.submitted_events()
        assert "age_restriction" not in sent
        assert _event(town)["age_restriction"] is None

    def test_a_picture_without_alt_text_is_not_sent(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert()])
        town.page_html[PAGE] = f'<html><head><meta property="og:image" content="{TRIO}"></head>'
        town.run()
        [sent] = town.submitted_events()
        assert "image_url" not in sent and "image_alt" not in sent

    def test_a_logo_is_not_an_event_picture(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert()])
        town.page_html[PAGE] = ('<meta property="og:image" content="https://arena.example/logo.png">'
                                '<meta property="og:image:alt" content="Lake Tahoe Arena wordmark">')
        town.run()
        assert "image_url" not in town.submitted_events()[0]

    def test_a_failed_server_fetch_still_ships_the_event(self, town):
        town.api.image_fetch_fails.add(TRIO)
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert()])
        town.page_html[PAGE] = OG
        town.run()
        [sent] = town.submitted_events()
        assert sent["image_url"] == TRIO
        assert _event(town)["state"] == "shipped"


class TestFromAFeed:
    def test_an_adapters_picture_is_submitted(self, town):
        from dataclasses import replace
        from engine import seed_adapters
        row = {**concert(source_url=None),
               **seed_adapters.feed_image(TRIO, f"<b>{TRIO_ALT}</b>")}
        town.mp.setitem(seed_adapters.ADAPTERS, "tribe", replace(
            seed_adapters.ADAPTERS["tribe"], read=lambda seed, config, get_json: [row]))
        town.seed("Arena", ARENA, adapter="tribe")
        town.run()
        [sent] = town.submitted_events()
        assert (sent["image_url"], sent["image_alt"]) == (TRIO, TRIO_ALT)


class TestFromTheListing:
    def test_the_listing_cards_own_picture(self, town):
        town.seed("Arena", ARENA)
        ev = concert(source_url=None)
        town.listing(ARENA, [ev])
        town.pages[ARENA] = town.pages[ARENA].replace(
            "Jazz Night", f"![{TRIO_ALT}]({TRIO})\n\nJazz Night", 1)
        town.run()
        [sent] = town.submitted_events()
        assert (sent["image_url"], sent["image_alt"]) == (TRIO, TRIO_ALT)

    def test_another_cards_picture_is_not_this_events(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert(source_url=None), concert("Comedy Hour", source_url=None)])
        town.pages[ARENA] = town.pages[ARENA].replace(
            "Comedy Hour", f"Comedy Hour ![A comedian at the mic]({TRIO})", 1)
        town.run()
        sent = {p["title"]: p for p in town.submitted_events()}
        assert "image_url" not in sent["Jazz Night"]
        assert sent["Comedy Hour"]["image_alt"] == "A comedian at the mic"

    def test_a_picture_never_changes_the_content_hash(self, town):
        town.seed("Arena", ARENA)
        ev = concert(source_url=None)
        upsert_event(town.conn, "Arena", ev)
        before = _event(town)["content_hash"]
        assert upsert_event(town.conn, "Arena", {**ev, "image_url": TRIO,
                                                 "image_alt": TRIO_ALT}) == "unchanged"
        row = _event(town)
        assert row["content_hash"] == before
        assert (row["image_url"], row["image_alt"]) == (TRIO, TRIO_ALT)

    def test_the_first_picture_stays(self, town):
        ev = concert(source_url=None, image_url=TRIO, image_alt=TRIO_ALT)
        upsert_event(town.conn, "Arena", ev)
        upsert_event(town.conn, "Arena", {**ev, "image_url": "https://cdn.example/b.jpg",
                                          "image_alt": "Someone else entirely"})
        assert _event(town)["image_url"] == TRIO


class TestFromAnotherCopy:
    def test_a_copys_picture_comes_with_its_own_alt(self, town):
        from engine import event_dedup
        ev = concert(source_url=None)
        upsert_event(town.conn, "Arena", ev)
        upsert_event(town.conn, "Chamber", {**ev, "image_url": TRIO, "image_alt": TRIO_ALT})
        rows = {r["source"]: dict(r) for r in town.conn.execute("SELECT * FROM events")}
        mine = rows["seed:Arena"]
        town.conn.execute("UPDATE events SET image_url = 'https://x.example/old.jpg' "
                          "WHERE event_uid = ?", (mine["event_uid"],))   # url-only (reconcile)
        mine["image_url"] = "https://x.example/old.jpg"
        event_dedup.best_of_copies(town.conn, mine)
        row = town.conn.execute("SELECT image_url, image_alt FROM events WHERE event_uid = ?",
                                (mine["event_uid"],)).fetchone()
        assert tuple(row) == (TRIO, TRIO_ALT)


class TestTypedFields:
    def test_a_date_only_event_is_all_day(self, ship, monkeypatch, conn):
        from tests.test_live_lookup import EV
        api = _use(monkeypatch, FakeAPI())
        _stage(ship, monkeypatch, conn, ev={**EV, "date": "2026-10-09"})
        assert ship.main() == 0
        [sent] = api.creates()
        assert sent["is_all_day"] is True
        assert api.events[api.created_slug()]["is_all_day"] is True

    def test_a_date_only_start_fills_a_live_rows_all_day(self, ship, monkeypatch, conn):
        from tests.test_live_lookup import EV
        start, _ = utc_times("2026-10-09", None)
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live", start=start)]))
        _stage(ship, monkeypatch, conn, ev={**EV, "date": "2026-10-09"})
        assert ship.main() == 0
        assert api.events["jazz-live"]["is_all_day"] is True

    def test_a_free_event_is_typed_free_and_tagged(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert()])
        town.pages[PAGE] = "# Jazz Night\n\nFree admission.\n\n" + "Live jazz. " * 20
        town.details[PAGE] = {"price_quote": "Free admission."}
        town.run()
        [sent] = town.submitted_events()
        assert sent["price_kind"] == "free" and "free" in sent["tags"]
        assert "price_min_cents" not in sent

    def test_ambiguous_prices_are_not_typed(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert()])
        town.pages[PAGE] = "# Jazz Night\n\n$15 advance, $20 at the door.\n\n" + "Jazz. " * 30
        town.details[PAGE] = {"price_quote": "$15 advance, $20 at the door."}
        town.run()
        [sent] = town.submitted_events()
        assert "price_kind" not in sent
        assert sent["description"].startswith("Tickets: $15–$20")


class TestAlreadyLive:
    """event-ship's fill of a live row (supervised ship path)."""

    def _with_image(self, conn, uid):
        conn.execute("UPDATE events SET image_url = ?, image_alt = ?, price = '$15' "
                     "WHERE event_uid = ?", (TRIO, TRIO_ALT, uid))

    def test_a_blank_live_picture_and_price_are_filled(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live")]))
        self._with_image(conn, _stage(ship, monkeypatch, conn))
        assert ship.main() == 0
        [patch] = api.patches()
        assert (patch["image_url"], patch["image_alt"]) == (TRIO, TRIO_ALT)
        assert (patch["price_kind"], patch["price_min_cents"]) == ("paid", 1500)
        live = api.events["jazz-live"]
        assert live["image_path"] and live["price_kind"] == "paid"

    def test_a_live_picture_and_price_are_never_replaced(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event(
            "jazz-live", image_path="/static/uploads/events/curated.jpg",
            image_alt="Curator's photo of the band", price_kind="paid", price_min_cents=2000)]))
        self._with_image(conn, _stage(ship, monkeypatch, conn))
        assert ship.main() == 0
        assert all("image_url" not in p and "price_kind" not in p for p in api.patches())
        assert api.fetched_images == []
        assert api.events["jazz-live"]["image_path"] == "/static/uploads/events/curated.jpg"

    def test_a_fill_the_server_could_not_fetch_is_not_a_mismatch(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live")], image_fetch_fails={TRIO}))
        uid = _stage(ship, monkeypatch, conn)
        self._with_image(conn, uid)
        assert ship.main() == 0
        assert not _row(conn, uid)["state"].startswith("stuck-")


class TestChangeSubmission:
    def test_a_picture_for_a_published_event_is_a_fill_not_part_of_the_change(self, town):
        new = soon(9)
        stage_change(town, {"start_at": {"live": "x", "source": utc_times(new, None)[0]}},
                     {}, date=new)
        town.conn.execute("UPDATE events SET image_url = ?, image_alt = ?", (TRIO, TRIO_ALT))
        town.conn.commit()
        town.run()
        [sent] = town.submitted_events()
        assert "image_url" not in sent and "image_alt" not in sent
        live = town.api.events["jazz-live"]
        assert live["image_alt"] == TRIO_ALT and live["start_at"] != sent["start_at"]
        assert town.api.fetched_images == [TRIO]
