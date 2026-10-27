"""Seed adapters (engine/seed_adapters.py): a seed with an adapter key is
read deterministically instead of through Firecrawl → LLM, and what it
returns goes through event-find's one upsert / cancellation / vanish /
source-health / judgement path."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from engine import seed_adapters
from engine.event_extract import event_uid

FIXTURES = Path(__file__).parent / "fixtures" / "adapters" / "tribe" / "fixture"
API = "https://shops.example/wp-json/tribe/events/v1/events"
SHOWARE = Path(__file__).parent / "fixtures" / "adapters" / "showare" / "fixture.json"
BOX_OFFICE = "https://valhallatahoe.showare.com"
PERF_LIST = BOX_OFFICE + "/include/widgets/events/performancelist.asp"


def tribe_pages() -> dict[int, dict]:
    return {int(p.stem.split("-")[1]): json.loads(p.read_text())
            for p in FIXTURES.glob("page-*.json")}


class FakeFeed:
    """A Tribe REST endpoint answering from the captured pages."""

    def __init__(self, pages=None, fail_on=None):
        self.pages = tribe_pages() if pages is None else pages
        self.fail_on = fail_on
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        page = int(params.get("page", 1))
        if page == self.fail_on:
            raise seed_adapters.AdapterError("http 503")
        return self.pages[page]


def tribe_seed(**kw) -> dict:
    return {"seed_id": 1, "name": "Heavenly Village", "url": "https://shops.example/events/",
            "adapter": "tribe", "adapter_config": None, "feed_type": None, "feed_url": None, **kw}


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------

class TestAdapterKey:
    @pytest.mark.parametrize("key", [None, "", "generic"])
    def test_unset_means_generic(self, key):
        assert seed_adapters.adapter_of({"adapter": key}) == "generic"
        assert seed_adapters.adapter_of({}) == "generic"

    def test_a_set_key_is_the_adapter(self):
        assert seed_adapters.adapter_of({"adapter": "tribe"}) == "tribe"

    def test_an_unknown_adapter_is_an_error_not_an_empty_listing(self):
        with pytest.raises(seed_adapters.AdapterError, match="unknown adapter"):
            seed_adapters.read(tribe_seed(adapter="nope"), get_json=FakeFeed())

    def test_malformed_config_is_an_error(self):
        with pytest.raises(seed_adapters.AdapterError, match="adapter_config"):
            seed_adapters.read(tribe_seed(adapter_config="{not json"), get_json=FakeFeed())


# ---------------------------------------------------------------------------
# Tribe REST
# ---------------------------------------------------------------------------

class TestTribe:
    def test_every_row_on_every_page_becomes_an_event(self):
        feed = FakeFeed()
        listing = seed_adapters.read(tribe_seed(), get_json=feed)
        assert [c[1]["page"] for c in feed.calls] == [1, 2, 3]
        rows = sum(len(p["events"]) for p in tribe_pages().values())
        # one occurrence repeats across a page boundary
        assert len(listing.events) == rows - 1
        titles = [e["title"] for e in listing.events]
        assert titles.count("Ice Skating Lessons") == 2     # each occurrence kept

    def test_recurring_occurrences_dedupe_on_id_and_start(self):
        listing = seed_adapters.read(tribe_seed(), get_json=FakeFeed())
        skates = [e for e in listing.events if e["title"] == "Ice Skating Lessons"]
        assert [e["date"] for e in skates] == ["2027-01-09T10:00:00", "2027-01-16T10:00:00"]

    def test_rows_are_normalized_like_an_extraction(self):
        listing = seed_adapters.read(tribe_seed(), get_json=FakeFeed())
        music = listing.events[0]
        assert music["title"] == "Live Music: The Mountain Boys & Friends"
        assert music["date"] == "2027-01-08T18:00:00"        # town-local wall clock
        assert music["end_at"] == "2027-01-08T21:00:00"
        assert music["venue"] == "The Shops at Heavenly Village"
        assert music["location"] == "1001 Heavenly Village Way, South Lake Tahoe, CA 96150"
        assert music["description"] == "Bluegrass on the plaza stage.\nFree."
        assert music["source_url"] == "https://shops.example/event/live-music/2027-01-08/"
        assert music["categories"] == ["Live Music"]

    def test_a_row_with_no_venue_has_none(self):
        listing = seed_adapters.read(tribe_seed(), get_json=FakeFeed())
        market = next(e for e in listing.events if e["title"] == "Winter Market")
        assert market["venue"] is None and market["location"] is None

    def test_an_all_day_row_is_date_only(self):
        # Tribe gives an all-day event 00:00–23:59:59; neither is a published time.
        pages = tribe_pages()
        row = {**pages[1]["events"][0], "all_day": True,
               "start_date": "2027-01-08 00:00:00", "end_date": "2027-01-08 23:59:59",
               "utc_start_date": "2027-01-08 08:00:00", "utc_end_date": "2027-01-09 07:59:59"}
        feed = FakeFeed({1: {**pages[1], "events": [row], "total_pages": 1}})
        [evt] = seed_adapters.read(tribe_seed(), get_json=feed).events
        assert (evt["date"], evt["end_at"]) == ("2027-01-08", None)

    def test_the_page_text_carries_each_rows_own_words(self):
        # what cancellation and judgement quote-check against
        listing = seed_adapters.read(tribe_seed(), get_json=FakeFeed())
        assert "CANCELLED: Snow Sculpture Contest" in listing.page
        assert "Cancelled due to lack of snow." in listing.page
        assert "The Shops at Heavenly Village" in listing.page

    def test_the_endpoint_comes_from_config_then_the_probed_feed_then_the_site(self):
        feed = FakeFeed()
        seed_adapters.read(tribe_seed(adapter_config=json.dumps({"url": "https://x.example/api"})),
                           get_json=feed)
        seed_adapters.read(tribe_seed(feed_type="tribe", feed_url=API), get_json=feed)
        seed_adapters.read(tribe_seed(), get_json=feed)
        urls = {c[0] for c in feed.calls[:3]}, {c[0] for c in feed.calls[3:6]}, \
            {c[0] for c in feed.calls[6:]}
        assert urls == ({"https://x.example/api"}, {API},
                        {"https://shops.example/wp-json/tribe/events/v1/events"})

    def test_it_asks_from_today_and_for_full_pages(self):
        feed = FakeFeed()
        seed_adapters.read(tribe_seed(adapter_config='{"days_ahead": 30}'), get_json=feed)
        params = feed.calls[0][1]
        assert params["per_page"] == 50
        assert len(params["start_date"]) == 10 and len(params["end_date"]) == 10

    def test_a_feed_that_grows_while_paged_is_read_to_its_end(self):
        pages = tribe_pages()
        pages[1]["total_pages"] = 2           # page 2's answer says there are 3
        feed = FakeFeed(pages)
        seed_adapters.read(tribe_seed(), get_json=feed)
        assert [c[1]["page"] for c in feed.calls] == [1, 2, 3]

    def test_a_row_without_a_title_is_kept_for_event_find_to_count(self):
        pages = tribe_pages()
        pages[3]["events"][0]["title"] = ""
        listing = seed_adapters.read(tribe_seed(), get_json=FakeFeed(pages))
        assert listing.events[-1]["title"] is None

    def test_a_page_that_fails_fails_the_whole_read(self):
        # partial coverage would count every unread event as missing
        with pytest.raises(seed_adapters.AdapterError, match="page 2"):
            seed_adapters.read(tribe_seed(), get_json=FakeFeed(fail_on=2))

    def test_an_answer_without_events_is_an_error(self):
        with pytest.raises(seed_adapters.AdapterError, match="events"):
            seed_adapters.read(tribe_seed(), get_json=FakeFeed(
                pages={1: {"code": "rest_forbidden", "message": "nope"}}))

    def test_more_pages_than_the_cap_is_an_error(self):
        with pytest.raises(seed_adapters.AdapterError, match="pages"):
            seed_adapters.read(tribe_seed(adapter_config='{"max_pages": 2}'),
                               get_json=FakeFeed())

    def test_an_empty_feed_is_an_empty_listing(self):
        listing = seed_adapters.read(tribe_seed(), get_json=FakeFeed(
            pages={1: {"events": [], "total": 0, "total_pages": 0}}))
        assert listing.events == []


# ---------------------------------------------------------------------------
# Showare performance list
# ---------------------------------------------------------------------------

def showare_capture() -> dict:
    """Valhalla Tahoe's performance list, captured 2026-10-22: 13
    performances of two events (8 + 5)."""
    return json.loads(SHOWARE.read_text())


class FakeShoware:
    """A Showare performancelist.asp answering from the capture, paged the
    way the live one is: `listPageSize` rows of page `page`, `perfCount`
    capped at `listMaxSize`."""

    def __init__(self, body=None, fail_on=None):
        self.body = showare_capture() if body is None else body
        self.fail_on = fail_on
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        page, size = int(params["page"]), int(params["listPageSize"])
        if page == self.fail_on:
            raise seed_adapters.AdapterError("showare no action")
        rows = self.body["performance"][:int(params["listMaxSize"])]
        return {**self.body, "performance": rows[(page - 1) * size:page * size],
                "perfCount": min(self.body["perfCount"], int(params["listMaxSize"]))}


def showare_seed(**kw) -> dict:
    return {"seed_id": 24, "name": "Valhalla Tahoe", "adapter": "showare",
            "url": PERF_LIST + "?action=perf&page=1&listPageSize=100&listMaxSize=100",
            "adapter_config": None, "feed_type": None, "feed_url": None, **kw}


def _shown_end(body: dict) -> dict:
    return {**body, "settings": {**body["settings"], "ShowEndTime": 1}}


class TestShoware:
    def test_every_performance_becomes_an_event(self):
        listing = seed_adapters.read(showare_seed(), get_json=FakeShoware())
        assert len(listing.events) == 13
        titles = [e["title"] for e in listing.events]
        assert titles.count("The Revolutionists") == 8
        assert titles.count("Guilty Christmas - Original Musical 2026") == 5

    def test_two_performances_of_one_event_stay_distinct_occurrences(self):
        listing = seed_adapters.read(showare_seed(), get_json=FakeShoware())
        plays = [e for e in listing.events if e["title"] == "The Revolutionists"]
        assert [e["date"] for e in plays[:2]] == ["2026-10-22T19:30:00", "2026-10-23T19:30:00"]
        uids = {event_uid("Valhalla Tahoe", e["title"], e["date"]) for e in plays}
        assert len(uids) == 8

    def test_rows_are_normalized_like_an_extraction(self):
        listing = seed_adapters.read(showare_seed(), get_json=FakeShoware())
        play = listing.events[0]
        assert play["title"] == "The Revolutionists"
        assert play["date"] == "2026-10-22T19:30:00"          # the box office's wall clock
        assert play["venue"] == "Valhalla Boathouse Theatre"
        # the feed's address repeats itself; it is kept once
        assert play["location"] == ("#1 Valhalla Rd., Emerald Bay Rd. (Highway 89), "
                                    "South Lake Tahoe, CA 96151")
        assert play["description"].startswith("The Revolutionists\nBy Lauren Gunderson\n")
        assert "Marie Antoinette" in play["description"] and "&rsquo;" not in play["description"]
        assert play["source_url"] == BOX_OFFICE + "/eventperformances.asp?evt=272"
        assert (play["categories"], play["tags"]) == ([], [])

    def test_an_event_the_box_office_redirects_is_read_at_its_redirect(self):
        body = showare_capture()
        body["performance"][0].update(RedirectionEnabled=1,
                                      RedirectURL="https://valhallatahoe.com/revolutionists")
        listing = seed_adapters.read(showare_seed(), get_json=FakeShoware(body))
        assert listing.events[0]["source_url"] == "https://valhallatahoe.com/revolutionists"
        # a redirect URL left behind with redirection off is ignored
        body["performance"][0]["RedirectionEnabled"] = 0
        listing = seed_adapters.read(showare_seed(), get_json=FakeShoware(body))
        assert listing.events[0]["source_url"] == BOX_OFFICE + "/eventperformances.asp?evt=272"

    def test_the_wall_clock_is_kept_across_the_dst_change(self):
        # Showare labels every time PST (offset -8); the clock is what the
        # box office shows, so November 1's 2 PM stays 2 PM after DST ends.
        listing = seed_adapters.read(showare_seed(), get_json=FakeShoware())
        matinee = [e for e in listing.events if e["title"] == "The Revolutionists"][-1]
        assert matinee["date"] == "2026-11-01T14:00:00"

    def test_an_end_time_the_box_office_hides_is_not_published(self):
        # settings.ShowEndTime 0: the widget never shows PerformanceEndDateTime
        listing = seed_adapters.read(showare_seed(), get_json=FakeShoware())
        assert {e["end_at"] for e in listing.events} == {None}

    def test_an_end_time_the_box_office_shows_is_kept(self):
        body = _shown_end(showare_capture())
        listing = seed_adapters.read(showare_seed(), get_json=FakeShoware(body))
        assert listing.events[0]["end_at"] == "2026-10-22T21:30:00"

    def test_a_start_time_alias_is_no_published_time(self):
        # e.g. "TBA" or "Doors at dusk" shown in place of the clock
        body = _shown_end(showare_capture())
        body["performance"][0]["PerformanceStartTimeAlias"] = "TBA"
        listing = seed_adapters.read(showare_seed(), get_json=FakeShoware(body))
        assert (listing.events[0]["date"], listing.events[0]["end_at"]) == ("2026-10-22", None)

    def test_pages_cover_a_perf_count_above_the_page_size(self, monkeypatch):
        monkeypatch.setattr(seed_adapters, "SHOWARE_PAGE_SIZE", 5)
        feed = FakeShoware()
        listing = seed_adapters.read(showare_seed(), get_json=feed)
        assert [c[1]["page"] for c in feed.calls] == [1, 2, 3]
        assert {c[1]["listPageSize"] for c in feed.calls} == {5}
        assert len(listing.events) == 13
        assert listing.events[-1]["date"] == "2026-11-22T19:30:00"

    def test_one_page_when_the_perf_count_fits(self):
        feed = FakeShoware()
        seed_adapters.read(showare_seed(), get_json=feed)
        assert [c[1]["page"] for c in feed.calls] == [1]

    def test_the_seed_urls_query_is_replaced_not_repeated(self):
        # performancelist.asp reads one `page`; a second would be "1, 2"
        feed = FakeShoware()
        seed_adapters.read(showare_seed(), get_json=feed)
        url, params = feed.calls[0]
        assert url == PERF_LIST
        assert params == {"action": "perf", "page": 1, "listPageSize": 100,
                          "listMaxSize": seed_adapters.SHOWARE_MAX_PERFORMANCES + 1}

    def test_the_endpoint_comes_from_config_then_the_probed_feed_then_the_site(self):
        feed = FakeShoware()
        seed_adapters.read(showare_seed(adapter_config=json.dumps(
            {"url": "https://other.showare.com/include/widgets/events/performancelist.asp"})),
            get_json=feed)
        seed_adapters.read(showare_seed(url="https://valhallatahoe.com/events/",
                                        feed_type="showare", feed_url=PERF_LIST), get_json=feed)
        seed_adapters.read(showare_seed(url=BOX_OFFICE + "/"), get_json=feed)
        assert [c[0] for c in feed.calls] == [
            "https://other.showare.com/include/widgets/events/performancelist.asp",
            PERF_LIST, PERF_LIST]

    def test_event_urls_are_on_the_endpoints_box_office(self):
        feed = FakeShoware()
        listing = seed_adapters.read(showare_seed(adapter_config=json.dumps(
            {"url": "https://other.showare.com/include/widgets/events/performancelist.asp"})),
            get_json=feed)
        assert listing.events[-1]["source_url"] == \
            "https://other.showare.com/eventperformances.asp?evt=274"

    def test_a_no_action_answer_is_a_failed_read(self, monkeypatch):
        class Resp:
            status_code, ok, text = 200, True, "no action"

            def json(self):
                raise ValueError("not JSON")
        monkeypatch.setattr("engine.seed_adapters.requests.get", lambda *a, **k: Resp())
        with pytest.raises(seed_adapters.AdapterError, match="page 1: showare no action"):
            seed_adapters.read(showare_seed())

    def test_a_page_that_fails_fails_the_whole_read(self, monkeypatch):
        monkeypatch.setattr(seed_adapters, "SHOWARE_PAGE_SIZE", 5)
        with pytest.raises(seed_adapters.AdapterError, match="page 2"):
            seed_adapters.read(showare_seed(), get_json=FakeShoware(fail_on=2))

    def test_fewer_performances_than_the_count_is_an_error(self, monkeypatch):
        # the live list answers an empty page past its end, endOfList false
        body = showare_capture()
        body["perfCount"] = 14
        with pytest.raises(seed_adapters.AdapterError, match="13 of 14"):
            seed_adapters.read(showare_seed(), get_json=FakeShoware(body))

    def test_a_list_that_shifts_while_paged_is_an_error(self, monkeypatch):
        # a list that shifted while paged repeats one row and drops another
        monkeypatch.setattr(seed_adapters, "SHOWARE_PAGE_SIZE", 5)
        body = showare_capture()
        rows = body["performance"]
        body["performance"] = rows[:5] + rows[4:12]
        with pytest.raises(seed_adapters.AdapterError, match="12 of 13"):
            seed_adapters.read(showare_seed(), get_json=FakeShoware(body))

    def test_an_answer_without_a_performance_list_is_an_error(self):
        with pytest.raises(seed_adapters.AdapterError, match="performance"):
            seed_adapters.read(showare_seed(), get_json=lambda url, params: {"error": "x"})

    def test_an_answer_without_a_count_is_an_error(self):
        body = showare_capture()
        del body["perfCount"]
        with pytest.raises(seed_adapters.AdapterError, match="perfCount"):
            seed_adapters.read(showare_seed(), get_json=lambda url, params: body)

    def test_more_performances_than_the_cap_is_an_error(self):
        feed = FakeShoware()
        with pytest.raises(seed_adapters.AdapterError, match="max_performances 10"):
            seed_adapters.read(showare_seed(adapter_config='{"max_performances": 10}'),
                               get_json=feed)
        assert feed.calls[0][1]["listMaxSize"] == 11      # asked for one more than the cap

    def test_a_row_without_a_title_is_kept_for_event_find_to_count(self):
        body = showare_capture()
        body["performance"][0]["Event"] = ""
        listing = seed_adapters.read(showare_seed(), get_json=FakeShoware(body))
        assert listing.events[0]["title"] is None and listing.events[0]["date"]

    def test_an_empty_list_is_an_empty_listing(self):
        body = {**showare_capture(), "performance": [], "perfCount": 0}
        assert seed_adapters.read(showare_seed(), get_json=FakeShoware(body)).events == []


# ---------------------------------------------------------------------------
# event-find dispatches on the seed's adapter
# ---------------------------------------------------------------------------

SEED = "Heavenly Village"


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    fresh_db.execute(
        "INSERT INTO event_seeds (name, url, kind, status, adapter, first_seen_at, last_seen_at) "
        "VALUES (?, 'https://shops.example/events/', 'venue', 'approved', 'tribe', 'x', 'x')",
        (SEED,))
    fresh_db.commit()
    return fresh_db


@pytest.fixture
def find(tmp_state_dir, monkeypatch):
    """event-find with the network stubbed: adapter seeds read `feed`, a
    generic seed would fail the test (no Firecrawl, no Codex)."""
    path = Path(__file__).resolve().parent.parent / "skills/business/event-find/find.py"
    spec = importlib.util.spec_from_file_location("event_find_adapters", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    feed = FakeFeed()
    judged: list = []

    def no_generic(*a, **k):
        raise AssertionError("adapter seed went down the generic path")

    monkeypatch.setattr(mod, "_DB_PATH", tmp_state_dir / "townloops.db")
    monkeypatch.setattr(mod, "_load_town_profile", lambda: {})
    monkeypatch.setattr(mod, "_firecrawl_listing", no_generic)
    monkeypatch.setattr(mod, "_codex_exec", no_generic)
    monkeypatch.setattr(mod, "_telegram", lambda text: None)
    monkeypatch.setattr("engine.seed_adapters.fetch_json", feed)
    monkeypatch.setattr("engine.inspection._DB_PATH", tmp_state_dir / "townloops.db")

    def run(dry_run=False):
        return mod.cmd_find(argparse.Namespace(seed_id=None, limit=None, dry_run=dry_run))
    run.mod, run.feed, run.judged = mod, feed, judged
    return run


def _events(conn):
    return {r["title"]: dict(r) for r in conn.execute("SELECT * FROM events")}


def _runs(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM source_runs ORDER BY id")]


class TestEventFindWiring:
    def test_adapter_events_are_upserted_pending_and_the_run_recorded(self, conn, find):
        assert find() == 0
        events = _events(conn)
        assert len(events) == 5                 # two skating occurrences, one row each title
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 6
        music = events["Live Music: The Mountain Boys & Friends"]
        assert (music["state"], music["approval_status"]) == ("metadata-extracted", "pending")
        assert music["source"] == f"seed:{SEED}"
        assert music["source_url"] == "https://shops.example/event/live-music/2027-01-08/"
        [run] = _runs(conn)
        assert (run["ok"], run["fetched"], run["inserted"]) == (1, 6, 6)
        seed = conn.execute("SELECT * FROM event_seeds").fetchone()
        assert seed["last_event_count"] == 6 and seed["last_scraped_at"]

    def test_a_listing_cancellation_on_the_feed_is_recorded(self, conn, find):
        find()
        row = _events(conn)["Snow Sculpture Contest"]      # marker off the title
        assert row["cancel_status"] == "possibly-cancelled"
        acts = conn.execute("SELECT target FROM agent_activity "
                            "WHERE action = 'listed-cancelled-flagged'").fetchall()
        assert [a[0] for a in acts] == [row["event_uid"]]

    def test_an_event_gone_from_the_feed_gains_a_miss(self, conn, find):
        find()
        find.feed.pages[3]["events"] = []
        find()
        rows = _events(conn)
        assert rows["Ice Skating Lessons"]["missed_finds"] == 0
        # the fireworks fall beyond the latest listed date, so aren't counted
        assert rows["New Year Fireworks"]["missed_finds"] == 0
        find.feed.pages[1]["events"] = find.feed.pages[1]["events"][1:]   # the live music
        find()
        assert _events(conn)["Live Music: The Mountain Boys & Friends"]["missed_finds"] == 1

    def test_autonomous_judgement_sees_the_adapter_page(self, conn, find, monkeypatch):
        from engine import autonomy
        calls = []
        monkeypatch.setattr(find.mod, "_series_checks", lambda c, s: (None, None, None))
        monkeypatch.setattr(autonomy, "judge_context", lambda *a, **k: None)
        monkeypatch.setattr(find.mod, "_judge", lambda c, rows, seen, page, ctx, llm: (
            calls.append((rows, seen, page)) or {"approved": 0, "dropped": 0, "deferred": 0}))
        monkeypatch.setattr(find.mod, "_load_town_profile",
                            lambda: {"operating_mode": "autonomous"})
        find()
        rows, seen, page = calls[0]
        assert len(rows) == 6 and len(seen) == 6
        assert "Cancelled due to lack of snow." in page

    def test_an_adapter_error_is_a_failed_run(self, conn, find):
        find.feed.fail_on = 2
        find()
        [run] = _runs(conn)
        assert run["ok"] == 0 and "http 503" in run["error"]
        assert _events(conn) == {}

    def test_an_adapter_that_raises_is_a_failed_run(self, conn, find, monkeypatch):
        def broken(seed, config, get_json):
            raise KeyError("venue")
        monkeypatch.setitem(seed_adapters.ADAPTERS, "tribe",
                            replace(seed_adapters.ADAPTERS["tribe"], read=broken))
        find()
        [run] = _runs(conn)
        assert run["ok"] == 0 and "KeyError" in run["error"]
        assert conn.execute("SELECT last_scraped_at FROM event_seeds").fetchone()[0] is None

    def test_a_town_adapter_that_raises_is_a_failed_run(self, conn, find, tmp_state_dir):
        adapters = tmp_state_dir.parent / "adapters"          # $HERMES_HOME/adapters
        adapters.mkdir()
        (adapters / "boom.py").write_text(
            "from engine.seed_adapters import Adapter\n"
            "def read(seed, config, get_json):\n    raise KeyError('venue')\n"
            "ADAPTER = Adapter(read, {})\n")
        (adapters / "broken.py").write_text("def read(:\n")
        conn.execute("INSERT INTO event_seeds (name, url, kind, status, adapter, first_seen_at, "
                     "last_seen_at) VALUES ('Town Hall', 'https://hall.example/', 'venue', "
                     "'approved', 'boom', 'x', 'x')")
        find()
        runs = {r["source"]: r for r in _runs(conn)}
        hall = runs["seed:Town Hall"]
        assert hall["ok"] == 0 and "boom: KeyError" in hall["error"]
        assert runs[f"seed:{SEED}"]["ok"] == 1                # the broken file harmed no one

    def test_a_failed_run_is_never_a_zero(self, conn, find):
        from engine import source_health
        for _ in range(4):
            find()
        find.feed.fail_on = 1
        find()
        assert source_health.flagged(conn) == []
        assert [r["ok"] for r in _runs(conn)] == [1, 1, 1, 1, 0]

    def test_dry_run_reads_the_adapter_and_writes_nothing(self, conn, find, capsys):
        find(dry_run=True)
        assert find.feed.calls                          # the feed was read
        assert _events(conn) == {} and _runs(conn) == []
        seed = conn.execute("SELECT * FROM event_seeds").fetchone()
        assert seed["last_scraped_at"] is None and seed["last_event_count"] is None
        out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert {"seed": SEED, "events_seen": 6} in [
            {k: o[k] for k in ("seed", "events_seen")} for o in out if "seed" in o]


class TestShowareThroughEventFind:
    @pytest.fixture
    def showare(self, conn, find, monkeypatch):
        conn.execute("UPDATE event_seeds SET adapter = 'showare', url = ?", (PERF_LIST,))
        monkeypatch.setattr("engine.seed_adapters.fetch_json", FakeShoware())
        return conn

    def test_town_local_times_land_in_utc(self, showare, find):
        assert find() == 0
        rows = {r["date_local"]: dict(r) for r in showare.execute(
            "SELECT * FROM events WHERE title = 'The Revolutionists'")}
        assert len(rows) == 8                       # one row per performance
        assert rows["2026-10-22T19:30:00"]["date_utc"] == "2026-10-23T02:30:00+00:00"   # PDT
        assert rows["2026-11-01T14:00:00"]["date_utc"] == "2026-11-01T22:00:00+00:00"   # PST
        [run] = _runs(showare)
        assert (run["ok"], run["fetched"], run["inserted"]) == (1, 13, 13)

    def test_a_no_action_answer_is_a_failed_run(self, showare, find, monkeypatch):
        def no_action(url, params):
            raise seed_adapters.AdapterError("showare no action")
        monkeypatch.setattr("engine.seed_adapters.fetch_json", no_action)
        find()
        [run] = _runs(showare)
        assert run["ok"] == 0 and "showare no action" in run["error"]
        assert _events(showare) == {}


class TestUidStability:
    def test_an_adapter_rows_uid_is_the_extraction_uid(self, conn, find):
        # a seed moving from generic to an adapter keeps its rows
        find()
        uid = event_uid(SEED, "Winter Market", "2027-01-23T09:00:00")
        assert conn.execute("SELECT 1 FROM events WHERE event_uid = ?", (uid,)).fetchone()



# ---------------------------------------------------------------------------
# Setting a seed's adapter
# ---------------------------------------------------------------------------

class TestParams:
    """Each adapter declares its settings (`Adapter.params`); a config is
    checked against them before anything is stored."""

    def test_the_declared_settings_pass(self):
        seed_adapters.check_config("tribe", {"url": API, "max_pages": 5, "days_ahead": 90})
        seed_adapters.check_config("tribe", {})
        seed_adapters.check_config("showare", {"url": PERF_LIST, "max_performances": 500})

    @pytest.mark.parametrize("config,named", [
        ({"nope": 1}, "nope"),
        ({"days_ahead": "soon"}, "days_ahead"),
        ({"days_ahead": 0}, "days_ahead"),
        ({"max_pages": True}, "max_pages"),
        ({"url": "ftp://shops.example/feed"}, "url"),
        ({"url": 5}, "url"),
    ])
    def test_an_unknown_setting_or_a_bad_value_is_refused_by_name(self, config, named):
        with pytest.raises(ValueError, match=named):
            seed_adapters.check_config("tribe", config)

    @pytest.mark.parametrize("config,named", [
        ({"days_ahead": 30}, "days_ahead"),
        ({"max_performances": 0}, "max_performances"),
        ({"url": "valhallatahoe.showare.com"}, "url"),
    ])
    def test_showare_refuses_what_it_does_not_declare(self, config, named):
        with pytest.raises(ValueError, match=named):
            seed_adapters.check_config("showare", config)

    def test_a_probed_feed_type_names_its_adapter(self):
        assert seed_adapters.for_feed("tribe") == "tribe"
        assert seed_adapters.for_feed("showare") == "showare"
        assert seed_adapters.for_feed("ical") is None
        assert seed_adapters.for_feed(None) is None

    def test_no_two_adapters_claim_one_feed_type(self):
        claimed = [t for a in seed_adapters.ADAPTERS.values() for t in a.feed_types]
        assert len(claimed) == len(set(claimed))


def _listing(n):
    return seed_adapters.Listing([{"title": f"Show {i}", "date": "2027-01-01T19:00:00"}
                                  for i in range(n)], "page")


class TestSetAdapter:
    def _seed(self, conn):
        return dict(conn.execute("SELECT * FROM event_seeds").fetchone())

    def _set(self, conn, adapter, config=None, read=lambda seed: _listing(2)):
        from engine import autonomy, seeds
        return seeds.set_adapter(conn, self._seed(conn), adapter, config,
                                 autonomy.Settings(mode="autonomous"), read_adapter=read)

    def test_a_known_adapter_and_its_config_are_stored_after_a_trial(self, conn):
        conn.execute("UPDATE event_seeds SET adapter = NULL")
        tried = []
        out = self._set(conn, "tribe", {"days_ahead": 90},
                        read=lambda seed: tried.append(seed) or _listing(3))
        seed = self._seed(conn)
        assert (seed["adapter"], seed["adapter_config"]) == ("tribe", '{"days_ahead": 90}')
        # the trial read the seed as it would be, before it was saved
        assert (tried[0]["adapter"], tried[0]["adapter_config"]) == ("tribe", '{"days_ahead": 90}')
        assert (out.changed, out.trial_events, out.status) == (True, 3, "approved")

    def test_generic_clears_both_without_a_trial(self, conn):
        self._set(conn, "tribe", {"days_ahead": 90})

        def no_trial(seed):
            raise AssertionError("generic needs no trial")
        out = self._set(conn, "generic", read=no_trial)
        seed = self._seed(conn)
        assert (seed["adapter"], seed["adapter_config"]) == (None, None)
        assert (out.changed, out.trial_events) == (True, None)

    def test_an_unknown_adapter_or_bad_config_is_refused(self, conn):
        with pytest.raises(ValueError, match="unknown adapter"):
            self._set(conn, "nope")
        with pytest.raises(ValueError, match="JSON object"):
            self._set(conn, "tribe", [1])
        with pytest.raises(ValueError, match="nope"):
            self._set(conn, "tribe", {"nope": 1})
        assert self._seed(conn)["adapter_config"] is None     # untouched

    @pytest.mark.parametrize("read,why", [
        (lambda seed: _listing(0), "no usable events"),
        (lambda seed: (_ for _ in ()).throw(seed_adapters.AdapterError("page 1: http 503")),
         "http 503"),
    ])
    def test_a_failed_or_empty_trial_leaves_the_seed_unchanged(self, conn, read, why):
        conn.execute("UPDATE event_seeds SET adapter = NULL")
        with pytest.raises(ValueError, match=why):
            self._set(conn, "tribe", {"days_ahead": 90}, read=read)
        seed = self._seed(conn)
        assert (seed["adapter"], seed["adapter_config"]) == (None, None)
