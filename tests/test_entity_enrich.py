"""engine.entity_enrich: budgeted research, fill-blanks, local → live → new resolution."""

from __future__ import annotations

import sqlite3

import pytest

from engine import entity_enrich as ee


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


@pytest.fixture
def net(monkeypatch):
    """Stub Firecrawl search + page research; record every network call."""
    calls = {"search": [], "research": []}

    class Res:
        def __init__(self, url):
            self.payload = {"name": "Site Name", "image_path": f"{url}/og.png",
                            "description": "From site", "phone": "555-0100",
                            "not_a_field": "x"}
            self.fetched_from = [url]

    def fake_research(url, use_firecrawl=False):
        calls["research"].append(url)
        return Res(url)

    def fake_search(q, limit=5, **kw):
        calls["search"].append(q)
        return [{"url": "https://pumpkinpatch.example", "title": "Pumpkin Patch Co"}]

    import engine.discovery as discovery
    import engine.research as research
    monkeypatch.setattr(research, "research_url", fake_research)
    monkeypatch.setattr(discovery, "firecrawl_search", fake_search)
    return calls


def _venue(conn, uid="v1", **fields):
    from engine.derive_helpers import insert_entity
    insert_entity(conn, "venues", uid, {"name": "Pumpkin Patch Co", "source": "test",
                                        "source_kind": "derived", **fields})
    return uid


EVENT = {"event_uid": "e" * 16, "category_slug": "family", "location": "1 Main St"}


class TestBudget:
    def test_take_until_exhausted(self):
        b = ee.Budget(2)
        assert [b.take(), b.take(), b.take()] == [True, True, False]
        assert b.left == 0

    def test_zero_budget_never_grants(self):
        assert ee.Budget(0).take() is False


class TestResearchEntity:
    def test_known_website_costs_one_lookup(self, net):
        b = ee.Budget(5)
        payload = ee.research_entity("Pumpkin Patch Co", "https://pp.example", "Tahoe", b)
        assert b.left == 4 and net["search"] == []
        assert payload["website"] == "https://pp.example"
        assert payload["phone"] == "555-0100"
        assert "name" not in payload and "not_a_field" not in payload

    def test_search_then_scrape_costs_two(self, net):
        b = ee.Budget(5)
        payload = ee.research_entity("Pumpkin Patch Co", None, "Tahoe", b)
        assert b.left == 3
        assert net["search"] == ['"Pumpkin Patch Co" Tahoe']
        assert payload["website"] == "https://pumpkinpatch.example"

    def test_no_budget_no_network(self, net):
        assert ee.research_entity("Pumpkin Patch Co", None, "Tahoe", ee.Budget(0)) == {}
        assert net == {"search": [], "research": []}

    def test_budget_runs_out_between_search_and_scrape(self, net):
        b = ee.Budget(1)
        assert ee.research_entity("Pumpkin Patch Co", None, "Tahoe", b) == {}
        assert len(net["search"]) == 1 and net["research"] == []

    def test_no_plausible_site_found(self, net, monkeypatch):
        import engine.discovery as discovery
        monkeypatch.setattr(discovery, "firecrawl_search",
                            lambda q, limit=5, **kw: [{"url": "https://unrelated.example", "title": "Other"}])
        assert ee.research_entity("Pumpkin Patch Co", None, "Tahoe", ee.Budget(5)) == {}
        assert net["research"] == []


class TestFillBlanks:
    def test_fills_only_blank_columns(self, conn):
        uid = _venue(conn, phone="555-9999")
        filled = ee.fill_blanks(conn, "venue", uid,
                                {"phone": "555-0100", "description": "New", "bogus": "x"}, "enriched")
        assert filled == ["description"]
        row = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (uid,)).fetchone()
        assert row["phone"] == "555-9999" and row["description"] == "New"
        assert row["enrichment_status"] == "enriched" and row["enriched_at"]

    def test_empty_payload_still_records_status(self, conn):
        uid = _venue(conn)
        before = conn.execute("SELECT last_modified_at FROM venues").fetchone()[0]
        assert ee.fill_blanks(conn, "venue", uid, {}, "not-found") == []
        row = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (uid,)).fetchone()
        assert row["enrichment_status"] == "not-found"
        assert row["last_modified_at"] == before


class TestResolveEntity:
    def test_local_match_without_image_is_enriched(self, conn, net):
        uid = _venue(conn, website="https://pp.example")
        r = ee.resolve_entity(conn, "venue", "Pumpkin Patch Co", EVENT, [], "Tahoe", ee.Budget(5))
        assert (r.entity_uid, r.outcome) == (uid, "local")
        row = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (uid,)).fetchone()
        assert row["image_path"] == "https://pp.example/og.png"
        assert row["enrichment_status"] == "enriched"

    def test_local_match_already_attempted_is_left_alone(self, conn, net):
        _venue(conn, enrichment_status="not-found")
        r = ee.resolve_entity(conn, "venue", "Pumpkin Patch Co", EVENT, [], "Tahoe", ee.Budget(5))
        assert r.outcome == "local" and net["research"] == []

    def test_live_match_with_image_mirrors_without_research(self, conn, net):
        live = [{"id": 7, "slug": "pumpkin-patch", "name": "Pumpkin Patch Co", "image_path": "/x.png"}]
        r = ee.resolve_entity(conn, "venue", "Pumpkin Patch Co", EVENT, live, "Tahoe", ee.Budget(5))
        assert r.outcome == "live" and r.image_proposal is None
        row = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (r.entity_uid,)).fetchone()
        assert row["source_kind"] == "live" and row["townloops_slug"] == "pumpkin-patch"
        assert net == {"search": [], "research": []}

    def test_live_match_without_image_proposes_image_only(self, conn, net):
        live = [{"id": 7, "slug": "pumpkin-patch", "name": "Pumpkin Patch Co",
                 "website": "https://pp.example", "image_path": None}]
        r = ee.resolve_entity(conn, "venue", "Pumpkin Patch Co", EVENT, live, "Tahoe", ee.Budget(5))
        assert r.image_proposal == {"kind": "venue", "slug": "pumpkin-patch",
                                    "image_path": "https://pp.example/og.png"}
        row = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (r.entity_uid,)).fetchone()
        assert row["enrichment_status"] == "image-proposed"
        assert row["phone"] is None  # live rows only get an image proposal

    def test_unknown_entity_creates_enriched_shell(self, conn, net):
        r = ee.resolve_entity(conn, "venue", "Pumpkin Patch Co", EVENT, [], "Tahoe", ee.Budget(5))
        assert r.outcome == "created" and "phone" in r.filled
        row = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (r.entity_uid,)).fetchone()
        assert row["source_kind"] == "derived" and row["address"] == "1 Main St"
        assert row["category_slug"] == "family" and row["enrichment_status"] == "enriched"

    def test_unknown_entity_out_of_budget_stays_pending(self, conn, net):
        r = ee.resolve_entity(conn, "community", "Rotary Club", EVENT, [], "Tahoe", ee.Budget(0))
        assert r.outcome == "created" and r.filled == []
        row = conn.execute("SELECT * FROM communities WHERE entity_uid = ?", (r.entity_uid,)).fetchone()
        assert row["enrichment_status"] == "pending" and row["kind"] == "group"

    def test_budget_running_out_between_search_and_scrape_stays_pending(self, conn, net):
        r = ee.resolve_entity(conn, "venue", "Pumpkin Patch Co", EVENT, [], "Tahoe", ee.Budget(1))
        row = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (r.entity_uid,)).fetchone()
        assert row["enrichment_status"] == "pending" and row["enriched_at"] is None

    def test_search_that_ran_and_found_nothing_is_not_found(self, conn, net, monkeypatch):
        import engine.discovery as discovery
        monkeypatch.setattr(discovery, "firecrawl_search", lambda q, limit=5, **kw: [])
        r = ee.resolve_entity(conn, "venue", "Pumpkin Patch Co", EVENT, [], "Tahoe", ee.Budget(5))
        row = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (r.entity_uid,)).fetchone()
        assert row["enrichment_status"] == "not-found"

    def test_pending_shell_is_researched_next_run(self, conn, net):
        first = ee.resolve_entity(conn, "venue", "Pumpkin Patch Co", EVENT, [], "Tahoe", ee.Budget(0))
        again = ee.resolve_entity(conn, "venue", "Pumpkin Patch Co", EVENT, [], "Tahoe", ee.Budget(5))
        assert (again.entity_uid, again.outcome) == (first.entity_uid, "local")
        row = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (first.entity_uid,)).fetchone()
        assert row["enrichment_status"] == "enriched" and row["phone"] == "555-0100"

    def test_local_match_whose_search_finds_nothing_is_not_found(self, conn, net, monkeypatch):
        import engine.discovery as discovery
        monkeypatch.setattr(discovery, "firecrawl_search", lambda q, limit=5, **kw: [])
        uid = _venue(conn)
        ee.resolve_entity(conn, "venue", "Pumpkin Patch Co", EVENT, [], "Tahoe", ee.Budget(5))
        row = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (uid,)).fetchone()
        assert row["enrichment_status"] == "not-found"


LIVE_NO_IMAGE = [{"id": 7, "slug": "pumpkin-patch", "name": "Pumpkin Patch Co",
                  "website": "https://pp.example", "image_path": None}]


class TestLiveMirrorResearch:
    """A live mirror is researched only for an image proposal, and a search
    that found no image backs off instead of repeating every week."""

    def _resolve(self, conn, budget):
        return ee.resolve_entity(conn, "venue", "Pumpkin Patch Co", EVENT, LIVE_NO_IMAGE,
                                 "Tahoe", ee.Budget(budget))

    def _row(self, conn, uid):
        return conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (uid,)).fetchone()

    def test_out_of_budget_mirror_is_retried_next_run(self, conn, net):
        uid = self._resolve(conn, 0).entity_uid
        assert self._row(conn, uid)["enrichment_status"] == "live"
        r = self._resolve(conn, 5)
        assert r.image_proposal == {"kind": "venue", "slug": "pumpkin-patch",
                                    "image_path": "https://pp.example/og.png"}
        row = self._row(conn, uid)
        assert row["enrichment_status"] == "image-proposed"
        assert row["image_path"] == "https://pp.example/og.png"

    def test_mirror_matched_locally_gets_only_an_image(self, conn, net):
        uid = self._resolve(conn, 0).entity_uid
        self._resolve(conn, 5)
        row = self._row(conn, uid)
        assert row["image_path"] and row["phone"] is None and row["description"] is None

    def test_no_image_found_backs_off(self, conn, net, monkeypatch):
        import engine.research as research

        class NoImage:
            def __init__(self, url):
                self.payload, self.fetched_from = {"phone": "555-0100"}, [url]

        monkeypatch.setattr(research, "research_url", lambda url, use_firecrawl=False: NoImage(url))
        uid = self._resolve(conn, 5).entity_uid
        row = self._row(conn, uid)
        assert row["enrichment_status"] == "image-not-found" and row["enriched_at"]
        assert row["phone"] is None

        calls = []
        monkeypatch.setattr(research, "research_url",
                            lambda url, use_firecrawl=False: calls.append(url))
        self._resolve(conn, 5)
        assert calls == []

    def test_backoff_expires(self, conn, net):
        uid = self._resolve(conn, 0).entity_uid
        conn.execute("UPDATE venues SET enrichment_status = 'image-not-found', enriched_at = ? "
                     "WHERE entity_uid = ?", ("2026-01-01T00:00:00+00:00", uid))
        self._resolve(conn, 5)
        assert self._row(conn, uid)["enrichment_status"] == "image-proposed"
        assert net["research"] == ["https://pp.example"]

    def test_pending_proposal_is_not_researched_again(self, conn, net):
        uid = self._resolve(conn, 5).entity_uid
        assert self._row(conn, uid)["enrichment_status"] == "image-proposed"
        conn.execute("UPDATE venues SET image_path = NULL WHERE entity_uid = ?", (uid,))
        net["research"].clear()
        self._resolve(conn, 5)
        assert net["research"] == []


# --------------------------------------------------------------------------
# enrich_entity — per-kind research of one entity from fixture pages
# --------------------------------------------------------------------------

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "enrich"

SITES = {
    "https://city.example/lakeview": "venue.html",
    "https://cuppa.example/": "business.html",
    "https://rotary.example/": "community.html",
    "https://cowboys.example/": "performer.html",
    "https://janemuir.example/": "creator.html",
    "https://www.facebook.com/sierrastrings": "facebook-page.html",
    "https://www.facebook.com/sierrastrings/about/": "facebook-about.html",
}


@pytest.fixture
def web(monkeypatch):
    """Fixture pages served by URL; every image verifies; search returns
    whatever the test puts in `web["search"]`."""
    import engine.discovery as discovery
    import engine.research as research
    state = {"fetched": [], "search": [], "queries": []}

    def fetch(url, timeout=20.0):
        state["fetched"].append(url)
        name = SITES.get(url)
        return ((FIXTURES / name).read_text(), url) if name else (None, None)

    def search(q, limit=5, **kw):
        state["queries"].append(q)
        return state["search"]

    monkeypatch.setattr(research, "fetch_html", fetch)
    monkeypatch.setattr(research, "verify_image", lambda url, timeout=10.0: True)
    monkeypatch.setattr(discovery, "firecrawl_search", search)
    return state


def _entity(conn, kind, name, **fields):
    from engine.derive_helpers import insert_entity
    uid = f"{kind}-{name}"
    insert_entity(conn, ee.SPECS[kind].table, uid,
                  {"name": name, "source": "test", "source_kind": "derived",
                   "enrichment_status": "pending", **fields})
    return uid


def _row(conn, kind, uid):
    return dict(conn.execute(f"SELECT * FROM {ee.SPECS[kind].table} WHERE entity_uid = ?",
                             (uid,)).fetchone())


class TestEnrichEntity:
    def test_venue_fills_address_phone_description_and_hero_image(self, conn, web):
        uid = _entity(conn, "venue", "Lakeview Commons", website="https://city.example/lakeview")
        out = ee.enrich_entity(conn, "venue", uid, "South Lake Tahoe", ee.Budget(5))
        assert out.status == "enriched"
        row = _row(conn, "venue", uid)
        assert row["address"] == "1077 Lakeview Ave, South Lake Tahoe, CA, 96150"
        assert row["phone"] == "+15305426000"
        # verbatim from the page, entities decoded — not rewritten
        assert row["description"] == ("Lakeview Commons is a lakefront park with a beach, "
                                      "a boat launch & an open lawn for summer concerts.")
        # og:image is the city logo → the hero photo instead
        assert row["image_path"] == "https://city.example/images/lakeview-commons-lawn.jpg"
        assert row["enrichment_status"] == "enriched" and row["enriched_at"]

    def test_business_found_by_search_gets_socials(self, conn, web):
        web["search"] = [{"url": "https://cuppa.example/", "title": "Cuppa Tahoe - Coffee"}]
        uid = _entity(conn, "business", "Cuppa Tahoe")
        out = ee.enrich_entity(conn, "business", uid, "South Lake Tahoe", ee.Budget(5))
        row = _row(conn, "business", uid)
        assert web["queries"] == ['"Cuppa Tahoe" South Lake Tahoe']
        assert row["website"] == "https://cuppa.example/"
        assert row["instagram"] == "https://www.instagram.com/cuppatahoe"
        assert row["facebook"] == "https://www.facebook.com/cuppatahoe"
        assert row["image_path"] == "https://cuppa.example/storefront.jpg"
        # JSON-LD gave a list of phones — not a valid phone value, dropped
        assert row["phone"] is None
        assert any("phone" in e for e in out.errors)

    def test_community_gets_description_and_socials(self, conn, web):
        uid = _entity(conn, "community", "Rotary Club of Tahoe", website="https://rotary.example/")
        ee.enrich_entity(conn, "community", uid, "Tahoe", ee.Budget(5))
        row = _row(conn, "community", uid)
        assert row["description"].startswith("Service above self")
        assert row["linkedin"] == "https://www.linkedin.com/company/tahoerotary"
        assert row["image_path"] == "https://rotary.example/members-at-cleanup.jpg"

    def test_performer_gets_spotify_and_instagram(self, conn, web):
        uid = _entity(conn, "performer", "The Reggae Cowboys", website="https://cowboys.example/")
        ee.enrich_entity(conn, "performer", uid, "Tahoe", ee.Budget(5))
        row = _row(conn, "performer", uid)
        assert row["spotify_url"] == "https://open.spotify.com/artist/abc"
        assert row["instagram"] == "https://www.instagram.com/reggaecowboys"
        assert row["description"] == "Roots reggae with a Sierra twang."

    def test_creator_description_lands_in_bio_and_youtube_in_youtube_url(self, conn, web):
        uid = _entity(conn, "creator", "Jane Muir", website="https://janemuir.example/")
        out = ee.enrich_entity(conn, "creator", uid, "Tahoe", ee.Budget(5))
        row = _row(conn, "creator", uid)
        assert out.status == "enriched"
        assert row["bio"] == "Landscape photographer documenting the Tahoe basin."
        assert row["youtube_url"] == "https://www.youtube.com/@janemuir"
        assert row["enrichment_status"] == "enriched"

    def test_facebook_page_also_reads_about_and_is_not_the_website(self, conn, web):
        uid = _entity(conn, "business", "Sierra Strings",
                      facebook="https://www.facebook.com/sierrastrings")
        ee.enrich_entity(conn, "business", uid, "Tahoe", ee.Budget(5))
        row = _row(conn, "business", uid)
        assert web["fetched"] == ["https://www.facebook.com/sierrastrings",
                                  "https://www.facebook.com/sierrastrings/about/"]
        assert row["phone"] == "+15305550199"
        assert row["address"] == "3040 US-50, Meyers, CA"
        assert row["image_path"] == "https://scontent.fbcdn.example/v/sierra-strings-profile.jpg"
        assert row["website"] is None

    def test_existing_values_are_never_overwritten(self, conn, web):
        uid = _entity(conn, "venue", "Lakeview Commons", website="https://city.example/lakeview",
                      description="Operator wrote this.", phone="530-555-0000")
        out = ee.enrich_entity(conn, "venue", uid, "Tahoe", ee.Budget(5))
        row = _row(conn, "venue", uid)
        assert row["description"] == "Operator wrote this." and row["phone"] == "530-555-0000"
        assert "description" not in out.filled and "address" in out.filled

    def test_live_mirror_is_left_alone(self, conn, web):
        uid = _entity(conn, "venue", "Lakeview Commons", website="https://city.example/lakeview",
                      source_kind="live", enrichment_status="live")
        out = ee.enrich_entity(conn, "venue", uid, "Tahoe", ee.Budget(5))
        assert out.status == "skipped" and web["fetched"] == []
        assert _row(conn, "venue", uid)["address"] is None

    def test_already_attempted_is_not_researched_again(self, conn, web):
        uid = _entity(conn, "venue", "Lakeview Commons", website="https://city.example/lakeview",
                      enrichment_status="not-found")
        assert ee.enrich_entity(conn, "venue", uid, "Tahoe", ee.Budget(5)).status == "skipped"
        assert web["fetched"] == []

    def test_not_found_is_recorded(self, conn, web):
        web["search"] = [{"url": "https://unrelated.example/", "title": "Something Else"}]
        uid = _entity(conn, "creator", "Nobody Known")
        out = ee.enrich_entity(conn, "creator", uid, "Tahoe", ee.Budget(5))
        assert out.status == "not-found" and out.filled == []
        assert _row(conn, "creator", uid)["enrichment_status"] == "not-found"

    def test_unreadable_site_defers_rather_than_not_found(self, conn, web):
        uid = _entity(conn, "venue", "Down Hall", website="https://down.example/")
        out = ee.enrich_entity(conn, "venue", uid, "Tahoe", ee.Budget(5))
        assert out.status == "deferred"
        assert _row(conn, "venue", uid)["enrichment_status"] == "pending"

    def test_search_error_defers_rather_than_not_found(self, conn, web, monkeypatch):
        import engine.discovery as discovery

        def broken(q, limit=5, raise_on_error=False):
            raise discovery.FirecrawlError("http 502")
        monkeypatch.setattr(discovery, "firecrawl_search", broken)
        uid = _entity(conn, "business", "Cuppa Tahoe")
        assert ee.enrich_entity(conn, "business", uid, "Tahoe", ee.Budget(5)).status == "deferred"
        assert _row(conn, "business", uid)["enrichment_status"] == "pending"

    def test_out_of_budget_defers_without_marking(self, conn, web):
        uid = _entity(conn, "business", "Cuppa Tahoe")
        out = ee.enrich_entity(conn, "business", uid, "Tahoe", ee.Budget(0))
        assert out.status == "deferred" and web["queries"] == [] and web["fetched"] == []
        assert _row(conn, "business", uid)["enrichment_status"] == "pending"

    def test_budget_running_out_mid_research_defers(self, conn, web):
        web["search"] = [{"url": "https://cuppa.example/", "title": "Cuppa Tahoe"}]
        uid = _entity(conn, "business", "Cuppa Tahoe")
        out = ee.enrich_entity(conn, "business", uid, "Tahoe", ee.Budget(1))
        assert out.status == "deferred" and web["fetched"] == []
        assert _row(conn, "business", uid)["enrichment_status"] == "pending"


class TestFetchPlan:
    def test_facebook_page_root_adds_about(self):
        assert ee.fetch_plan("https://www.facebook.com/cuppa/?ref=page") == [
            "https://www.facebook.com/cuppa/?ref=page", "https://www.facebook.com/cuppa/about/"]

    def test_other_pages_are_read_alone(self):
        for url in ("https://www.facebook.com/profile.php?id=1",
                    "https://www.facebook.com/cuppa/about/",
                    "https://www.facebook.com/events/123",
                    "https://notfacebook.com/cuppa",
                    "https://cuppa.example/"):
            assert ee.fetch_plan(url) == [url]


class TestValidatePayload:
    def test_valid_payload_passes_through(self):
        clean, errors = ee.validate_payload("venue", {"name": "X", "phone": "+1530",
                                                      "image_path": "https://x.example/a.jpg"})
        assert clean == {"name": "X", "phone": "+1530", "image_path": "https://x.example/a.jpg"}
        assert errors == []

    def test_wrong_types_and_bad_uris_are_dropped(self):
        clean, errors = ee.validate_payload("business", {
            "name": "X", "phone": ["1", "2"], "image_path": "/relative.jpg",
            "website": "not a url", "instagram": {"handle": "x"}})
        assert clean == {"name": "X"}
        assert len(errors) == 4

    def test_every_kind_spec_fills_only_schema_or_table_fields(self, conn):
        import json
        schema = json.loads((Path(ee.__file__).parent / "entity_payload.schema.json").read_text())
        assert set(ee.SPECS) == set(schema["properties"]["kind"]["enum"])
        for kind, spec in ee.SPECS.items():
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info('{spec.table}')")}
            assert set(spec.fillable) <= cols, kind


# --------------------------------------------------------------------------
# event-enrich-entities skill
# --------------------------------------------------------------------------

def _load_skill():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "skills/business/event-enrich-entities/enrich.py"
    spec = importlib.util.spec_from_file_location("event_enrich_entities", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestEnrichEntitiesSkill:
    def _event(self, conn, links):
        from engine.derive_helpers import link_event_to_entity
        from engine.event_extract import upsert_event
        upsert_event(conn, "Seed", {"title": "Reggae on the Lawn", "date": "2026-10-16T18:00:00",
                                    "source_url": "https://seed.example/reggae"})
        uid = conn.execute("SELECT event_uid FROM events").fetchone()[0]
        conn.execute("UPDATE events SET state = 'people-derived' WHERE event_uid = ?", (uid,))
        for kind, entity_uid, role in links:
            link_event_to_entity(conn, uid, kind, entity_uid, role=role)
        return uid

    def _run(self, conn, monkeypatch, uid, budget=None):
        mod = _load_skill()
        monkeypatch.setattr(mod, "open_db", lambda: conn)
        monkeypatch.setattr(mod, "parse_event_uid_arg", lambda: uid)
        monkeypatch.setenv("TOWN_PROFILE_PATH", "/nonexistent/town-profile.yaml")
        if budget is not None:
            monkeypatch.setenv("TOWNLOOPS_ENRICH_MAX_LOOKUPS", str(budget))
        return mod.main()

    def _activity(self, conn, uid):
        return [dict(r) for r in conn.execute(
            "SELECT action, detail, ok FROM agent_activity WHERE worker = 'event-enrich-entities' "
            "AND target = ?", (uid,))]

    def test_venue_and_hosts_enriched_performers_left_for_their_ladder(self, conn, web, monkeypatch):
        venue = _entity(conn, "venue", "Lakeview Commons", website="https://city.example/lakeview")
        band = _entity(conn, "performer", "The Reggae Cowboys", website="https://cowboys.example/")
        host = _entity(conn, "community", "Rotary Club of Tahoe", website="https://rotary.example/")
        uid = self._event(conn, [("venue", venue, "primary"), ("performer", band, "performer"),
                                 ("community", host, "host")])
        assert self._run(conn, monkeypatch, uid) == 0
        assert _row(conn, "venue", venue)["address"].startswith("1077 Lakeview Ave")
        assert _row(conn, "community", host)["enrichment_status"] == "enriched"
        # Performers go to performer-research (evidence ladder), not name search.
        assert _row(conn, "performer", band)["spotify_url"] is None
        assert _row(conn, "performer", band)["enrichment_status"] == "pending"
        activity = self._activity(conn, uid)
        assert [a["action"] for a in activity] == ["enriched"]
        assert "left for performer-research" in activity[0]["detail"]

    def test_entity_not_found_still_exits_zero_and_records_it(self, conn, web, monkeypatch):
        web["search"] = []
        venue = _entity(conn, "venue", "Mystery Barn")
        uid = self._event(conn, [("venue", venue, "primary")])
        assert self._run(conn, monkeypatch, uid) == 0
        assert _row(conn, "venue", venue)["enrichment_status"] == "not-found"
        assert "not-found" in self._activity(conn, uid)[0]["detail"]
        state = conn.execute("SELECT state FROM events WHERE event_uid = ?", (uid,)).fetchone()[0]
        assert not state.startswith("stuck-")

    def test_venue_inside_a_property_borrows_the_property_image(self, conn, web, monkeypatch):
        venue = _entity(conn, "venue", "Cuppa Back Room", website="https://cuppa.example/back-room")
        host = _entity(conn, "business", "Cuppa Tahoe", website="https://cuppa.example/")
        uid = self._event(conn, [("venue", venue, "primary"), ("business", host, "host")])
        assert self._run(conn, monkeypatch, uid) == 0
        row = _row(conn, "venue", venue)
        assert row["image_path"] == "https://cuppa.example/storefront.jpg"
        assert row["image_alt"]

    def test_property_image_not_borrowed_across_sites(self, conn, web, monkeypatch):
        venue = _entity(conn, "venue", "Some Hall", website="https://hall.example/")
        host = _entity(conn, "business", "Cuppa Tahoe", website="https://cuppa.example/")
        uid = self._event(conn, [("venue", venue, "primary"), ("business", host, "host")])
        self._run(conn, monkeypatch, uid)
        assert _row(conn, "venue", venue)["image_path"] is None

    def test_shared_platform_is_not_a_property(self, conn, web, monkeypatch):
        venue = _entity(conn, "venue", "Some Hall", website="https://www.facebook.com/somehall")
        host = _entity(conn, "business", "Sierra Strings",
                       facebook="https://www.facebook.com/sierrastrings",
                       website="https://www.facebook.com/sierrastrings",
                       image_path="https://scontent.fbcdn.example/x.jpg")
        uid = self._event(conn, [("venue", venue, "primary"), ("business", host, "host")])
        self._run(conn, monkeypatch, uid)
        assert _row(conn, "venue", venue)["image_path"] is None

    def test_exhausted_budget_defers_but_advances(self, conn, web, monkeypatch):
        venue = _entity(conn, "venue", "Lakeview Commons", website="https://city.example/lakeview")
        uid = self._event(conn, [("venue", venue, "primary")])
        assert self._run(conn, monkeypatch, uid, budget=0) == 0
        assert _row(conn, "venue", venue)["enrichment_status"] == "pending"
        assert web["fetched"] == []
        assert "deferred" in self._activity(conn, uid)[0]["detail"]

    def test_unknown_event_is_an_error(self, conn, web, monkeypatch):
        assert self._run(conn, monkeypatch, "nope") == 2
