"""Issue #64: source hunting. Seeds came only from a weekly Codex prompt
answered from model memory (8 of 10 rejected). `event-seed-discovery hunt`
looks for them daily from evidence — web search, the websites of
organisations the town already knows, the sites events link to — within a
Firecrawl budget, never re-proposing a source the town already has or
turned down, and proposes them for the same day's trial (`review`)."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3

import pytest

from tests.autonomy_town import PROFILE, SKILLS, Town, load, soon

LIBRARY = "https://library.example/calendar"


def concert(title="Jazz Night", days=7, **kw):
    return {"title": title, "date": soon(days), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live music.",
            "categories": ["music"], "tags": [], "source_url": None, **kw}


class Hunt:
    """The hunt's network: search results by query prefix, site pages (with
    their links) for the calendar mining, and every Firecrawl call made."""

    def __init__(self, town: Town):
        self.town = town
        self.results: dict[str, list[dict]] = {}
        self.sites: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.memory: list[dict] = []

    def search(self, query: str) -> list[dict]:
        self.calls.append(("search", query))
        return next((v for k, v in self.results.items() if query.startswith(k)), [])

    def scrape(self, url: str) -> dict:
        self.calls.append(("scrape", url))
        return self.sites.get(url.rstrip("/")) or self.sites.get(url) or {}

    def verify(self, url: str):
        self.calls.append(("verify", url))
        ok = url in self.town.pages or url.rstrip("/") in self.sites
        return ok, 200 if ok else 404, "<html></html>", url

    def codex(self, prompt: str, timeout_s: int = 0):
        if prompt.startswith("You are helping a local-events editor"):
            return True, json.dumps(self.memory), ""
        return self.town._codex(prompt, timeout_s)

    def run(self, *argv: str) -> dict:
        mod = load(SKILLS / "event-seed-discovery" / "discover.py", "town_seed_hunt")
        mp = self.town.mp
        mp.setattr(mod, "_DB_PATH", self.town.state / "townloops.db")
        mp.setattr(mod, "_hunt_search", self.search)
        mp.setattr(mod, "_site_scrape", self.scrape)
        mp.setattr(mod, "_scrape_page", self.verify)
        mp.setattr(mod, "_codex_exec", self.codex)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert mod.main(["hunt", *argv]) == 0
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def searched(self) -> list[str]:
        return [q for kind, q in self.calls if kind == "search"]


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


@pytest.fixture
def hunt(town):
    return Hunt(town)


def seeds(town) -> dict[str, dict]:
    return {r["url"]: dict(r) for r in town.conn.execute("SELECT * FROM event_seeds")}


def entity(town, table: str, name: str, website: str, **cols) -> None:
    data = {"entity_uid": f"{table}:{name}", "source": "test", "source_kind": "derived",
            "name": name, "website": website, "first_seen_at": "x", "last_seen_at": "x",
            "content_hash": "h", "last_modified_at": "x", **cols}
    town.conn.execute(f"INSERT INTO {table} ({', '.join(data)}) "
                      f"VALUES ({', '.join('?' * len(data))})", tuple(data.values()))
    town.conn.commit()


class TestSearch:
    def test_a_calendar_found_by_search_is_proposed_and_trialled_the_same_day(self, town, hunt):
        hunt.results["South Lake Tahoe library events"] = [
            {"url": LIBRARY, "title": "Library Calendar | Lake Library",
             "description": "Upcoming programs at the library."},
            {"url": "https://www.eventbrite.com/d/ca--south-lake-tahoe/events/",
             "title": "Events in South Lake Tahoe | Eventbrite"},
            {"url": "https://travel.example/blog/best-things-to-do-in-tahoe",
             "title": "25 Best Things To Do in Tahoe"},
            {"url": "https://library.example/about", "title": "About the Library"}]
        town.listing(LIBRARY, [concert("Story Time"), concert("Book Club", days=9)])

        out = hunt.run()

        assert list(seeds(town)) == [LIBRARY]   # no aggregator, no article, no non-calendar
        seed = seeds(town)[LIBRARY]
        assert (seed["status"], seed["discovered_by"], seed["kind"]) == \
            ("proposed", "search", "library")
        assert out["proposed"] == 1
        [d] = town.decisions("seed")
        assert d["verdict"] == "propose"
        assert d["evidence"]["query"].startswith("South Lake Tahoe library events")

        town.seed_review()
        assert seeds(town)[LIBRARY]["status"] == "approved"

    def test_a_source_the_town_already_has_or_turned_down_is_not_proposed_again(
            self, town, hunt):
        town.seed("Library", "https://library.example/events", status="rejected")
        town.seed("Arena", "https://arena.example/shows", status="retired")
        hunt.results["South Lake Tahoe"] = [
            {"url": LIBRARY, "title": "Library Calendar"},
            {"url": "https://www.arena.example/calendar/", "title": "Arena Calendar"}]
        town.listing(LIBRARY, [concert()])
        town.listing("https://www.arena.example/calendar/", [concert()])
        hunt.run()
        assert set(seeds(town)) == {"https://library.example/events", "https://arena.example/shows"}

    def test_queries_rotate_so_successive_days_search_something_new(self, town, hunt):
        hunt.run("--max-firecrawl", "3", "--inputs", "search")
        first = hunt.searched()
        hunt.calls.clear()
        hunt.run("--max-firecrawl", "3", "--inputs", "search")
        assert len(first) == 3 and not set(first) & set(hunt.searched())


class TestKnownOrganisations:
    def test_a_known_venues_own_calendar_page_is_proposed(self, town, hunt):
        entity(town, "venues", "Lake Tahoe Arena", "https://arena.example")
        entity(town, "businesses", "Quiet Cafe", "https://quietcafe.example")
        hunt.sites["https://arena.example"] = {
            "markdown": "Lake Tahoe Arena", "title": "Lake Tahoe Arena",
            "links": ["https://arena.example/about", "https://arena.example/events",
                      "https://arena.example/events/jazz-night-2026",
                      "https://tickets.example/arena"]}
        hunt.sites["https://quietcafe.example"] = {
            "markdown": "Quiet Cafe", "title": "Quiet Cafe",
            "links": ["https://quietcafe.example/menu"]}
        hunt.sites["https://arena.example/events"] = {"markdown": "Events"}

        hunt.run("--inputs", "sites")

        seed = seeds(town)["https://arena.example/events"]
        assert (seed["status"], seed["discovered_by"], seed["kind"]) == \
            ("proposed", "site", "venue")
        assert seed["name"] == "Lake Tahoe Arena"
        assert len(seeds(town)) == 1

        hunt.calls.clear()   # a site already mined isn't scraped again tomorrow
        hunt.run("--inputs", "sites")
        assert hunt.calls == []

    def test_a_listing_page_website_is_never_mined(self, town, hunt):
        entity(town, "businesses", "Yelp Listed", "https://www.yelp.com/biz/yelp-listed")
        hunt.run("--inputs", "sites")
        assert hunt.calls == [] and seeds(town) == {}


class TestEventDomains:
    def test_a_site_events_keep_linking_to_becomes_a_proposed_seed(self, town, hunt):
        town.seed("Visit Tahoe", "https://visit.example/events")
        for i in range(3):
            row = {"event_uid": f"e{i}", "source": "seed:Visit Tahoe", "source_kind": "seed",
                   "name": f"Show {i}", "title": f"Show {i}", "date": soon(5 + i),
                   "date_day": soon(5 + i)[:10], "first_seen_at": "x", "last_seen_at": "x",
                   "content_hash": "h", "last_modified_at": "x",
                   "source_url": f"https://theatre.example/events/show-{i}"}
            town.conn.execute(f"INSERT INTO events ({', '.join(row)}) "
                              f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        town.conn.commit()
        for i in range(3):   # a feed host serves many organisations: never a seed
            row = {"event_uid": f"g{i}", "source": "seed:Visit Tahoe", "source_kind": "seed",
                   "name": f"Talk {i}", "title": f"Talk {i}", "date": soon(5),
                   "date_day": soon(5)[:10], "first_seen_at": "x", "last_seen_at": "x",
                   "content_hash": "h", "last_modified_at": "x",
                   "source_url": f"https://calendar.google.com/calendar/event?eid={i}"}
            town.conn.execute(f"INSERT INTO events ({', '.join(row)}) "
                              f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        town.conn.commit()
        hunt.sites["https://theatre.example/events"] = {"markdown": "Shows"}

        hunt.run("--inputs", "domains")

        seed = seeds(town)["https://theatre.example/events"]
        assert (seed["status"], seed["discovered_by"]) == ("proposed", "domain")
        assert len(seeds(town)) == 2      # Visit Tahoe + the theatre; no Google Calendar seed
        [d] = town.decisions("seed")
        assert d["evidence"]["events"] == 3


class TestBudget:
    def test_the_run_stops_at_its_firecrawl_budget(self, town, hunt):
        for i in range(6):
            entity(town, "venues", f"Venue {i}", f"https://venue{i}.example")
        hunt.run("--max-firecrawl", "4", "--inputs", "sites,search")
        assert len(hunt.calls) == 4


class TestCalendarPath:
    @pytest.mark.parametrize("url,calendar", [
        ("https://arena.example/events/jazz-night", "https://arena.example/events"),
        ("https://arena.example/events/month/", "https://arena.example/events/month/"),
        ("https://city.example/Calendar.aspx?CID=14", "https://city.example/Calendar.aspx?CID=14"),
        ("https://club.example/whats-on", "https://club.example/whats-on"),
        ("https://club.example/about", None),
        ("https://club.example/", None),
    ])
    def test_a_page_gives_its_listing_level_calendar(self, url, calendar):
        from engine.seed_hunt import calendar_path
        assert calendar_path(url) == calendar


class TestMemory:
    def test_the_memory_proposer_is_one_input_run_weekly(self, town, hunt):
        hunt.memory = [{"name": "Lake Library", "url": LIBRARY, "kind": "library"}]
        town.listing(LIBRARY, [concert()])
        hunt.run("--inputs", "memory")
        assert seeds(town)[LIBRARY]["discovered_by"] == "llm"
        hunt.memory = [{"name": "Arena", "url": "https://arena.example/events", "kind": "venue"}]
        town.listing("https://arena.example/events", [concert()])
        hunt.run("--inputs", "memory")
        assert "https://arena.example/events" not in seeds(town)   # not again within the week
