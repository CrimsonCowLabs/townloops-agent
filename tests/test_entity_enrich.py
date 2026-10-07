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

    def fake_search(q, limit=5):
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
                            lambda q, limit=5: [{"url": "https://unrelated.example", "title": "Other"}])
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

    def test_unknown_entity_out_of_budget_is_not_found(self, conn, net):
        r = ee.resolve_entity(conn, "community", "Rotary Club", EVENT, [], "Tahoe", ee.Budget(0))
        assert r.outcome == "created" and r.filled == []
        row = conn.execute("SELECT * FROM communities WHERE entity_uid = ?", (r.entity_uid,)).fetchone()
        assert row["enrichment_status"] == "not-found" and row["kind"] == "group"
