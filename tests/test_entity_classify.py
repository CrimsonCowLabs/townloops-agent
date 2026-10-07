"""engine.entity_classify: keyword map → constrained LLM fallback, fill-blank only."""

from __future__ import annotations

import json
import sqlite3

import pytest

from engine import entity_classify as ec
from engine import entity_enrich as ee


SLT_CATEGORIES = [
    {"id": 6, "slug": "arts", "name": "Arts", "description": "Galleries, openings, theater, dance."},
    {"id": 5, "slug": "community", "name": "Community", "description": "Town meetings, volunteer days."},
    {"id": 3, "slug": "family", "name": "Family", "description": "Family-friendly events."},
    {"id": 2, "slug": "food-drink", "name": "Food & Drink", "description": "Tastings, pop-ups."},
    {"id": 1, "slug": "live-music", "name": "Live Music", "description": "Concerts, gigs."},
    {"id": 4, "slug": "outdoors", "name": "Outdoors", "description": "Trails, paddle days."},
]
ALLOWED = {c["slug"] for c in SLT_CATEGORIES}


@pytest.fixture
def snapshot(tmp_state_dir):
    (tmp_state_dir / "categories.json").write_text(json.dumps(SLT_CATEGORIES))
    return tmp_state_dir / "categories.json"


@pytest.fixture
def conn(fresh_db, snapshot):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


class FakeLLM:
    """Records prompts; answers with a canned reply."""

    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.reply


def _insert(conn, table, uid, **fields):
    from engine.derive_helpers import insert_entity
    insert_entity(conn, table, uid, {"source": "test", "source_kind": "derived", **fields})
    return uid


def _category(conn, table, uid):
    return conn.execute(f"SELECT category_slug FROM {table} WHERE entity_uid = ?",
                        (uid,)).fetchone()[0]


class TestKeywordCategory:
    def test_clear_case_maps_onto_town_slug(self):
        assert ec.keyword_category("Sunrise Cafe", "coffee and pastries", ALLOWED) == "food-drink"

    @pytest.mark.parametrize("name,desc,slug", [
        ("Tahoe Paddle & Kayak", "rentals on the beach", "outdoors"),
        ("Lake Tahoe Art Gallery", "", "arts"),
        ("South Tahoe Rotary Club", "a nonprofit service club", "community"),
        ("Valhalla Amphitheater", "summer concert series", "live-music"),
    ])
    def test_each_town_slug_reachable(self, name, desc, slug):
        assert ec.keyword_category(name, desc, ALLOWED) == slug

    def test_whole_words_only(self):
        # "bar" in barber, "ski" in whiskey, "inn" in dinner, "park" in parking.
        assert ec.keyword_category("Classic Barber", "whiskey-scented dinner parking", ALLOWED) is None

    def test_conflicting_hints_are_inconclusive(self):
        assert ec.keyword_category("Lakeside Brewery", "live music every friday", ALLOWED) is None

    def test_no_hint_is_inconclusive(self):
        assert ec.keyword_category("Foo", "asdfghjkl", ALLOWED) is None

    def test_hint_for_a_slug_the_town_lacks_is_inconclusive(self):
        assert ec.keyword_category("Sunrise Cafe", "", {"arts", "outdoors"}) is None
        # SLT has no lodging slug: a ski *resort* isn't clearly `outdoors`.
        assert ec.keyword_category("Heavenly Ski Resort", "", ALLOWED) is None

    def test_apostrophe_is_not_a_word_boundary(self):
        assert ec.keyword_category("Trail's End Diner", "", ALLOWED) == "food-drink"

    def test_deterministic(self):
        results = {ec.keyword_category("Sunrise Cafe", "coffee", ALLOWED) for _ in range(5)}
        assert results == {"food-drink"}


class TestLLMCategory:
    def test_prompt_offers_only_town_slugs(self):
        llm = FakeLLM("food-drink")
        assert ec.llm_category("Mystery Spot", "a place", "https://x.example",
                               SLT_CATEGORIES, llm) == "food-drink"
        prompt = llm.prompts[0]
        for slug in ALLOWED:
            assert slug in prompt
        assert "Mystery Spot" in prompt

    def test_reads_the_answer_past_agent_noise(self):
        llm = FakeLLM("thinking...\nI considered arts.\nSlug: `outdoors`\n")
        assert ec.llm_category("X", "", "", SLT_CATEGORIES, llm) == "outdoors"

    def test_invented_slug_is_rejected(self):
        assert ec.llm_category("X", "", "", SLT_CATEGORIES, FakeLLM("lodging")) is None

    def test_none_answer(self):
        assert ec.llm_category("X", "", "", SLT_CATEGORIES, FakeLLM("none")) is None

    def test_failed_call(self):
        assert ec.llm_category("X", "", "", SLT_CATEGORIES, FakeLLM(None)) is None

    def test_no_vocabulary_no_call(self):
        llm = FakeLLM("arts")
        assert ec.llm_category("X", "", "", [], llm) is None
        assert llm.prompts == []


class TestCodexLLM:
    def test_caps_calls(self):
        calls = []

        def run(prompt, timeout_s):
            calls.append(timeout_s)
            return (True, "arts", "")

        llm = ec.codex_llm(max_calls=2, timeout_s=30, deadline=float("inf"), run=run)
        assert [llm("p"), llm("p"), llm("p")] == ["arts", "arts", None]
        assert calls == [30, 30]

    def test_no_call_that_could_outrun_the_deadline(self):
        import time
        calls = []
        llm = ec.codex_llm(max_calls=2, timeout_s=30, deadline=time.monotonic() + 10,
                           run=lambda p, timeout_s: calls.append(p) or (True, "arts", ""))
        assert llm("p") is None
        assert calls == []

    def test_failure_is_none(self):
        llm = ec.codex_llm(max_calls=1, timeout_s=30, deadline=float("inf"),
                           run=lambda p, timeout_s: (False, "", "429 rate limit"))
        assert llm("p") is None


class TestClassifyEntity:
    def test_keyword_hit_fills_blank_without_llm(self, conn):
        uid = _insert(conn, "businesses", "b1", name="Sunrise Cafe", description="coffee")
        llm = FakeLLM("arts")
        assert ec.classify_entity(conn, "business", uid, llm) == "food-drink"
        assert _category(conn, "businesses", uid) == "food-drink"
        assert llm.prompts == []

    def test_llm_fallback_when_keywords_inconclusive(self, conn):
        uid = _insert(conn, "venues", "v1", name="The Loft", description="dinner and a show",
                      website="https://loft.example")
        llm = FakeLLM("live-music")
        assert ec.classify_entity(conn, "venue", uid, llm) == "live-music"
        assert _category(conn, "venues", uid) == "live-music"
        assert "https://loft.example" in llm.prompts[0]

    def test_inconclusive_without_llm_leaves_blank(self, conn):
        uid = _insert(conn, "venues", "v1", name="The Loft")
        assert ec.classify_entity(conn, "venue", uid) is None
        assert _category(conn, "venues", uid) is None

    def test_never_overwrites_a_category(self, conn):
        uid = _insert(conn, "businesses", "b1", name="Sunrise Cafe", category_slug="arts")
        llm = FakeLLM("outdoors")
        assert ec.classify_entity(conn, "business", uid, llm) is None
        assert _category(conn, "businesses", uid) == "arts"
        assert llm.prompts == []

    def test_unknown_slug_still_counts_as_set(self, conn):
        # Old rows may hold a slug the town doesn't have (e.g. `music`).
        uid = _insert(conn, "businesses", "b1", name="Sunrise Cafe", category_slug="music")
        assert ec.classify_entity(conn, "business", uid) is None
        assert _category(conn, "businesses", uid) == "music"

    def test_live_mirror_untouched(self, conn):
        uid = _insert(conn, "businesses", "b1", name="Sunrise Cafe", source_kind="live")
        assert ec.classify_entity(conn, "business", uid) is None
        assert _category(conn, "businesses", uid) is None

    def test_kind_without_category_column(self, conn):
        uid = _insert(conn, "performers", "p1", name="Cafe Band")
        assert ec.classify_entity(conn, "performer", uid, FakeLLM("arts")) is None

    def test_no_snapshot_no_write(self, conn, snapshot):
        snapshot.unlink()
        uid = _insert(conn, "businesses", "b1", name="Sunrise Cafe")
        assert ec.classify_entity(conn, "business", uid, FakeLLM("arts")) is None
        assert _category(conn, "businesses", uid) is None

    def test_refreshes_content_hash(self, conn):
        uid = _insert(conn, "businesses", "b1", name="Sunrise Cafe")
        before = conn.execute("SELECT content_hash FROM businesses WHERE entity_uid = ?",
                              (uid,)).fetchone()[0]
        ec.classify_entity(conn, "business", uid)
        after = conn.execute("SELECT content_hash FROM businesses WHERE entity_uid = ?",
                             (uid,)).fetchone()[0]
        assert after and after != before


@pytest.fixture
def net(monkeypatch):
    class Res:
        def __init__(self, url):
            self.payload = {"description": "Craft coffee roasted daily."}
            self.fetched_from = [url]

    import engine.research as research
    monkeypatch.setattr(research, "research_url", lambda url, use_firecrawl=False: Res(url))


class TestEnrichEntityClassifies:
    def test_classifies_from_researched_description(self, conn, net):
        uid = _insert(conn, "businesses", "b1", name="Sunrise", website="https://sunrise.example",
                      enrichment_status="pending")
        out = ee.enrich_entity(conn, "business", uid, "Tahoe", ee.Budget(3))
        assert out.status == "enriched"
        assert "category_slug" in out.filled
        assert _category(conn, "businesses", uid) == "food-drink"

    def test_llm_reaches_the_classifier(self, conn, net):
        uid = _insert(conn, "venues", "v1", name="The Loft", website="https://loft.example",
                      enrichment_status="pending")
        conn.execute("UPDATE venues SET description = 'dinner and a show' WHERE entity_uid = ?", (uid,))
        llm = FakeLLM("arts")
        ee.enrich_entity(conn, "venue", uid, "Tahoe", ee.Budget(3), llm=llm)
        assert _category(conn, "venues", uid) == "arts"

    def test_existing_category_kept(self, conn, net):
        uid = _insert(conn, "businesses", "b1", name="Sunrise", website="https://sunrise.example",
                      enrichment_status="pending", category_slug="arts")
        out = ee.enrich_entity(conn, "business", uid, "Tahoe", ee.Budget(3))
        assert "category_slug" not in out.filled
        assert _category(conn, "businesses", uid) == "arts"

    def test_already_researched_blank_category_is_retried(self, conn, net):
        # A throttled Codex call (or the per-event cap) left it blank last time.
        uid = _insert(conn, "venues", "v1", name="The Loft", enrichment_status="enriched")
        out = ee.enrich_entity(conn, "venue", uid, "Tahoe", ee.Budget(3), llm=FakeLLM("arts"))
        assert out.status == "skipped"
        assert out.filled == ["category_slug"]
        assert _category(conn, "venues", uid) == "arts"

    def test_live_mirror_not_classified(self, conn, net):
        uid = _insert(conn, "businesses", "b1", name="Sunrise Cafe", source_kind="live")
        out = ee.enrich_entity(conn, "business", uid, "Tahoe", ee.Budget(3))
        assert out.filled == []
        assert _category(conn, "businesses", uid) is None

    def test_deferred_does_not_classify(self, conn, net):
        uid = _insert(conn, "businesses", "b1", name="Sunrise Cafe", website="https://sunrise.example",
                      enrichment_status="pending")
        out = ee.enrich_entity(conn, "business", uid, "Tahoe", ee.Budget(0))
        assert out.status == "deferred"
        assert _category(conn, "businesses", uid) is None
