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

FIXTURES = Path(__file__).parent / "fixtures" / "tribe"
API = "https://shops.example/wp-json/tribe/events/v1/events"


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
    monkeypatch.setattr("engine.seed_adapters._get_json", feed)
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

    def test_a_probed_feed_type_names_its_adapter(self):
        assert seed_adapters.for_feed("tribe") == "tribe"
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
