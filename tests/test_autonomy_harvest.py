"""Autonomous mode, organisations, people, cancellations and the digest
(issue #1): discovered businesses, community groups and non-profits are
verified, deduped against TownLoops (published, pending and rejected) and
submitted pending; hosts and performers come from quote-checked AI reads;
a cancellation goes to TownLoops only with explicit page evidence; and the
nightly digest says what happened. Driven through the business-discovery,
event-cancellations, series-candidates and digest entry points."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3

import pytest

from tests.autonomy_town import PROFILE, SKILLS, Town, load, page_text, soon

ARENA = "https://arena.example/events"


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


def concert(title="Jazz Night", days=7, **kw):
    return {"title": title, "date": soon(days), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live music.",
            "categories": ["music"], "tags": [], "source_url": None, **kw}


def discovery(town, *argv):
    mod = load(SKILLS / "business-discovery" / "discover.py", "town_business_discovery")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert mod.main(list(argv)) == 0
    return out.getvalue()


@pytest.fixture
def harvest(town, monkeypatch):
    """Web search + site pages for the discovery kernel; entity gate on."""
    town.api.entity_status = "pending"
    profile = {**PROFILE, "discovery": {
        "business": {"category_query_terms": ["coffee shops"]},
        "nonprofit": {"category_query_terms": ["nonprofit organizations"]}}}
    town.set_profile(profile, entity_moderation=True)
    results: dict[str, list[dict]] = {}
    sites: dict[str, str] = {}

    def search(q, limit=5, raise_on_error=False):
        return next((v for k, v in results.items() if q.startswith(k)), [])

    def scrape(url, raise_on_error=False):
        md = sites.get(url.rstrip("/")) or sites.get(url)
        return {"markdown": md, "links": [], "title": ""} if md else {}
    monkeypatch.setattr("engine.discovery.firecrawl_search", search)
    monkeypatch.setattr("engine.discovery.firecrawl_scrape", scrape)
    monkeypatch.setattr("engine.discovery.BETWEEN_QUERIES_S", 0)
    def fetch_html(url, timeout=20.0):
        md = sites.get(url.rstrip("/"))
        return (f"<html><body><p>{md}</p></body></html>", url) if md else (None, None)
    monkeypatch.setattr("engine.research.fetch_html", fetch_html)
    monkeypatch.setattr("engine.research.firecrawl_fetch", lambda url: (None, None))
    monkeypatch.setenv("TOWNLOOPS_ENRICH_MAX_LOOKUPS", "4")
    return results, sites


def posts(town, resource):
    return [c[2] for c in town.api.calls if c[:2] == ("POST", resource)]


class TestOrganisations:
    def test_discovered_organisations_are_verified_deduped_and_submitted_pending(
            self, town, harvest):
        results, sites = harvest
        town.api.tables["businesses"] = {
            "cuppa": {"id": 1, "slug": "cuppa", "name": "Cuppa Coffee",
                      "website": "https://cuppa.example", "status": "pending"},
            "shady": {"id": 2, "slug": "shady", "name": "Shady Biz",
                      "website": "https://shady.example", "status": "rejected"}}
        results["coffee shops in"] = [
            {"url": "https://cuppa.example/", "title": "Cuppa Coffee"},
            {"url": "https://shady.example/", "title": "Shady Biz"},
            {"url": "https://newcafe.example/", "title": "New Cafe"}]
        results["nonprofit organizations in"] = [
            {"url": "https://lakefriends.org/", "title": "Friends of the Lake"}]
        sites["https://newcafe.example"] = "New Cafe in South Lake Tahoe. See our menu and hours."
        sites["https://lakefriends.org"] = ("Friends of the Lake is a nonprofit in South Lake "
                                            "Tahoe. Volunteer with us or donate today.")
        discovery(town, "harvest")
        discovery(town, "promote", "--limit", "5")

        assert [p["name"] for p in posts(town, "businesses")] == ["New Cafe"]
        [community] = posts(town, "communities")
        assert (community["name"], community["kind"]) == ("Friends of the Lake", "nonprofit")
        assert town.api.tables["businesses"][posts(town, "businesses")[0]["slug"]]["status"] \
            == "pending"
        assert {d["reason"] for d in town.decisions("candidate")} == {"verified-on-own-site"}

    def test_a_merged_organisation_is_repointed_to_the_survivor_the_404_names(
            self, town, harvest):
        # The survivor's name differs, so only the 404's merged_into_slug finds it.
        results, sites = harvest
        town.api.tables["businesses"] = {
            "nouveau": {"id": 9, "slug": "nouveau", "name": "Nouveau Cafe & Bakery",
                        "website": "https://nouveau.example", "status": "published"}}
        results["coffee shops in"] = [{"url": "https://newcafe.example/", "title": "New Cafe"}]
        sites["https://newcafe.example"] = "New Cafe in South Lake Tahoe. See our menu and hours."
        discovery(town, "harvest")
        discovery(town, "promote", "--limit", "5")
        [sent] = posts(town, "businesses")
        town.api.merge("businesses", sent["slug"], into="nouveau", fields=("website",))
        out = town.poll()
        assert (out["merged"], out["gone"]) == (1, 0)
        assert town.conn.execute("SELECT townloops_slug FROM businesses WHERE name = 'New Cafe'"
                                 ).fetchone()[0] == "nouveau"

    def test_entities_are_submitted_by_default(self, town, harvest):
        results, sites = harvest
        town.set_profile({k: v for k, v in town.profile.items() if k != "autonomy"})
        results["coffee shops in"] = [{"url": "https://newcafe.example/", "title": "New Cafe"}]
        sites["https://newcafe.example"] = "New Cafe in South Lake Tahoe. See our menu and hours."
        discovery(town, "harvest")
        discovery(town, "promote", "--limit", "5")
        assert [p["name"] for p in posts(town, "businesses")] == ["New Cafe"]

    def test_a_rejected_organisation_is_not_resubmitted_under_a_new_url(self, town, harvest):
        results, sites = harvest
        results["coffee shops in"] = [{"url": "https://newcafe.example/", "title": "New Cafe"}]
        sites["https://newcafe.example"] = "New Cafe in South Lake Tahoe. See our menu and hours."
        discovery(town, "harvest")
        discovery(town, "promote")
        [sent] = posts(town, "businesses")
        town.api.reject("businesses", sent["slug"])
        town.poll()

        results["coffee shops in"] = [{"url": "https://newcafe-tahoe.example/", "title": "New Cafe"}]
        sites["https://newcafe-tahoe.example"] = "New Cafe, South Lake Tahoe. Menu, hours."
        discovery(town, "harvest")
        discovery(town, "promote")
        assert len(posts(town, "businesses")) == 1

    def test_without_the_entity_gate_nothing_is_approved(self, town, harvest):
        results, sites = harvest
        town.set_profile(town.profile, entity_moderation=False)
        results["coffee shops in"] = [{"url": "https://newcafe.example/", "title": "New Cafe"}]
        sites["https://newcafe.example"] = "New Cafe in South Lake Tahoe. See our menu and hours."
        discovery(town, "harvest")
        discovery(town, "promote")
        assert posts(town, "businesses") == []

    def test_a_held_promote_spends_no_attempt(self, town, harvest):
        results, sites = harvest
        results["coffee shops in"] = [{"url": "https://newcafe.example/", "title": "New Cafe"}]
        sites["https://newcafe.example"] = "New Cafe in South Lake Tahoe. See our menu and hours."
        discovery(town, "harvest")
        town.set_profile(town.profile, entity_moderation=False)
        out = json.loads(discovery(town, "promote"))
        assert posts(town, "businesses") == []
        assert out["held"] == "entity-moderation-off" and out["failed"] == 0
        row = town.conn.execute("SELECT status, promote_attempts, promote_error "
                                "FROM discovery_candidates").fetchone()
        assert (row["status"], row["promote_attempts"], row["promote_error"]) == ("approved", 0, None)

    def test_harvest_is_inactive_in_supervised_mode(self, town, harvest):
        town.set_profile({**town.profile, "operating_mode": "supervised"})
        assert '"inactive": true' in discovery(town, "harvest")


class TestHostsAndPeople:
    def test_a_quoted_nonprofit_host_and_performer_are_linked(self, town):
        town.api.entity_status = "pending"
        town.set_profile(PROFILE, entity_moderation=True)
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Benefit Concert", description=(
            "Lake Tahoe Rotary Club presents the Mountain Ramblers. Proceeds support the "
            "club's scholarship fund."))])
        town.people["Benefit Concert"] = {
            "host": {"name": "Lake Tahoe Rotary Club", "kind": "community",
                     "kind_quote": "Lake Tahoe Rotary Club presents"},
            "people": [{"name": "the Mountain Ramblers", "role": "performer"},
                       {"name": "DJ Invented", "role": "performer"}]}
        town.run()
        [community] = posts(town, "communities")
        assert (community["name"], community["kind"]) == ("Lake Tahoe Rotary Club", "group")
        assert [p["name"] for p in posts(town, "performers")] == ["the Mountain Ramblers"]

    def test_a_host_the_text_does_not_name_is_null(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        town.people["Jazz Night"] = {"host": {"name": "Invented Org", "kind": "business",
                                              "kind_quote": None}, "people": []}
        town.run()
        assert town.conn.execute("SELECT derived_host_uid FROM events").fetchone()[0] is None
        assert town.submitted_titles() == ["Jazz Night"]


def cancellations(town, *argv):
    mod = load(SKILLS / "event-cancellations" / "cancellations.py", "town_cancellations")
    town.mp.setattr("engine.event_extract.firecrawl_markdown", town._fetch)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert mod.main(list(argv)) == 0
    return out.getvalue()


class TestCancellations:
    def _vanish(self, town, title, url):
        """Ship `title`, then four finds that list a later event but not it."""
        town.listing(ARENA, [concert(title, source_url=url), concert("Later", days=30)])
        town.run()
        town.listing(ARENA, [concert("Later", days=30)])
        for _ in range(4):
            town.find()
        town.api.approve("events", next(p["slug"] for p in town.submitted_events()
                                        if p["title"] == title))

    def test_a_cancellation_is_submitted_only_with_explicit_page_evidence(self, town):
        town.seed("Arena", ARENA)
        self._vanish(town, "Jazz Night", "https://arena.example/jazz")
        town.pages["https://arena.example/jazz"] = page_text(
            "Jazz Night", "This event has been cancelled due to weather.")
        before = len(town.submitted_events())
        cancellations(town, "apply")
        sent = town.submitted_events()[before:]
        assert len(sent) == 1
        assert sent[0]["title"] == "Cancelled: Jazz Night" and "cancelled" in sent[0]["tags"]
        assert town.api.patches() == [] or all(
            "title" not in p for p in town.api.patches())

    def test_a_vanished_event_without_a_statement_is_only_listed(self, town):
        town.seed("Arena", ARENA)
        self._vanish(town, "Jazz Night", "https://arena.example/jazz")
        town.pages["https://arena.example/jazz"] = page_text("Jazz Night", "Doors at 7pm.")
        before = len(town.submitted_events())
        cancellations(town, "apply")
        assert len(town.submitted_events()) == before
        _, md, tg = town.digest()
        assert "possibly cancelled (not acted on): Jazz Night" in md
        assert "possibly cancelled" in tg

    def test_operator_cancellation_proposals_are_inactive(self, town):
        assert '"inactive": true' in cancellations(town, "propose-confirm")


class TestDigest:
    def test_the_digest_sums_up_submissions_drops_seeds_and_series(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night"), concert("Old Show", days=-2),
                             concert("Blues Night", days=9)])
        town.run()
        town.api.reject("events", next(p["slug"] for p in town.submitted_events()
                                       if p["title"] == "Blues Night"))
        town.api.approve("events", next(p["slug"] for p in town.submitted_events()
                                        if p["title"] == "Jazz Night"))
        town.poll()
        # The weekly Series-candidates run saw a Cluster with no Series.
        start1, start2 = (f"2026-11-0{d}T03:00:00+00:00" for d in (5, 6))
        for slug, start in (("open-mic-1", start1), ("open-mic-2", start2)):
            town.api.events[slug] = {"id": hash(slug) % 1000, "slug": slug, "title": "Open Mic",
                                     "start_at": start, "status": "published", "venue_id": 5,
                                     "venue_slug": "the-loft", "series_slug": None}
        mod = load(SKILLS / "series-candidates" / "report.py", "town_series_candidates")
        with contextlib.redirect_stdout(io.StringIO()):
            mod.run(town.api, town.conn, 90, 400)

        out, md, tg = town.digest()
        a = out["autonomy"]
        assert a["events_submitted"] == 2 and a["events_dropped"] == 1
        assert a["top_drop_reasons"] == [["past", 1]] or a["top_drop_reasons"] == [("past", 1)]
        assert a["moderation"]["published"] == 1 and a["moderation"]["rejected"] == 1
        assert "## Series candidates" in md and "Open Mic" in md
        assert "dropped: 1 — top: past ×1" in tg
        assert "Awaiting approval" in md   # the shared header stays

    def test_supervised_digest_has_no_autonomy_section(self, town):
        town.set_profile({**PROFILE, "operating_mode": "supervised"})
        out, md, tg = town.digest()
        assert "autonomy" not in out and "Autonomous decisions" not in md
        assert "pending approval" in tg


class TestOperatorSkills:
    @pytest.mark.parametrize("skill,entry,argv", [
        ("operator-bulk-approve", "approve.py", ["--source", "x"]),
        ("operator-bulk-decline", "decline.py", ["--source", "x"]),
    ])
    def test_supervised_only_skills_say_they_are_inactive(self, town, skill, entry, argv):
        mod = load(SKILLS / skill / entry, f"town_{skill}")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert mod.main(argv) == 0
        assert json.loads(out.getvalue())["inactive"] is True

    def test_why_explains_a_drop(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Old Show", days=-2)])
        town.run()
        mod = load(SKILLS / "autonomy" / "autonomy.py", "town_autonomy")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert mod.main(["why", "Old Show"]) == 0
        [d] = json.loads(out.getvalue())["decisions"]
        assert (d["verdict"], d["reason"], d["title"]) == ("drop", "past", "Old Show")

    def test_resume_closes_the_circuit(self, town):
        from engine import autonomy
        autonomy.open_circuit(town.conn, "rejection-rate", {}, alert=lambda t: None)
        mod = load(SKILLS / "autonomy" / "autonomy.py", "town_autonomy_resume")
        with contextlib.redirect_stdout(io.StringIO()):
            assert mod.main(["resume"]) == 0
        assert autonomy.circuit(town.conn) is None


class TestCancellationWording:
    @pytest.mark.parametrize("page,verdict", [
        ("Jazz Night\n\nThis event has been cancelled due to weather.", "submit"),
        ("CANCELLED\n\nJazz Night with the Trio", "submit"),
        ("Jazz Night is postponed to spring", "submit"),
        ("Refunds are issued if an event is cancelled.", "hold"),
        ("Jazz Night will not be cancelled, rain or shine.", "hold"),
        ("Our classes are never cancelled for snow", "hold"),
    ])
    def test_only_an_explicit_statement_about_this_event_counts(self, page, verdict):
        from engine import autonomy
        assert autonomy.cancellation_evidence(page, "Jazz Night").verdict == verdict


class TestRestoredSeeds:
    def test_a_restored_seed_is_judged_on_a_fresh_record(self, fresh_db):
        from engine import autonomy
        s = autonomy.Settings(mode="autonomous")
        fresh_db.execute("INSERT INTO event_seeds (name, url, kind, status, first_seen_at, "
                         "last_seen_at, status_changed_at) VALUES ('A', 'u', 'venue', "
                         "'approved', 'x', 'x', '2099-01-01')")
        for i in range(6):
            fresh_db.execute("INSERT INTO submissions (resource, slug, seed, status, submitted_at,"
                             " decided_at) VALUES ('events', ?, 'A', 'rejected', '2020', '2020')",
                             (f"s{i}",))
        seed = dict(zip(("name", "status_changed_at"), ("A", "2099-01-01")))
        assert autonomy.judge_seed(fresh_db, seed, s).verdict == "keep"
