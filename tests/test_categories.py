"""Events carry the town's live category slugs (several per event) plus free
tags, from extraction through to the payload event-ship sends."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from engine import categories as cats
from engine.event_extract import build_extraction_prompt, content_hash, event_uid, upsert_event
from tests.fake_townloops import FakeAPI


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
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

class TestNormalizeCategorySlugs:
    def test_live_slugs_pass_through_in_order(self):
        assert cats.normalize_category_slugs(["outdoors", "live-music"], ALLOWED) == [
            "outdoors", "live-music"]

    def test_extraction_vocabulary_maps_onto_live_slugs(self):
        assert cats.normalize_category_slugs(["music", "food", "art"], ALLOWED) == [
            "live-music", "food-drink", "arts"]

    def test_case_spacing_and_comma_strings_are_tolerated(self):
        assert cats.normalize_category_slugs(" Live Music, OUTDOORS ", ALLOWED) == [
            "live-music", "outdoors"]

    def test_unknown_slugs_are_dropped(self):
        assert cats.normalize_category_slugs(["government", "sports", "other"], ALLOWED) == []

    def test_duplicates_after_mapping_collapse(self):
        assert cats.normalize_category_slugs(["music", "live-music"], ALLOWED) == ["live-music"]

    def test_capped_at_the_api_maximum(self):
        assert len(cats.normalize_category_slugs(sorted(ALLOWED) * 2, ALLOWED)) == 6

    def test_ambiguous_word_matching_two_slugs_is_dropped(self):
        allowed = {"live-music", "music-lessons"}
        assert cats.normalize_category_slugs(["music"], allowed) == []

    def test_none_and_junk_give_empty(self):
        assert cats.normalize_category_slugs(None, ALLOWED) == []
        assert cats.normalize_category_slugs([None, "", 3], ALLOWED) == []


class TestNormalizeTags:
    def test_tags_become_api_slugs(self):
        assert cats.normalize_tags(["Dog Friendly", "free", " 21+ "]) == [
            "dog-friendly", "free", "21"]

    def test_duplicates_and_blanks_are_dropped(self):
        assert cats.normalize_tags(["Free", "free", "", "  ", "!!"]) == ["free"]

    def test_comma_string_is_split(self):
        assert cats.normalize_tags("free, all ages") == ["free", "all-ages"]

    def test_capped_at_the_api_maximum(self):
        assert len(cats.normalize_tags([f"tag {i}" for i in range(15)])) == 10


class TestTownCategories:
    def test_reads_the_state_snapshot(self, snapshot):
        assert [c["slug"] for c in cats.town_categories()] == [c["slug"] for c in SLT_CATEGORIES]

    def test_missing_snapshot_is_empty(self, tmp_state_dir):
        assert cats.town_categories() == []


# --------------------------------------------------------------------------
# extraction → DB
# --------------------------------------------------------------------------

def _row(conn, title="Jazz Night", date="2026-10-09T20:00:00"):
    return conn.execute("SELECT * FROM events WHERE event_uid = ?",
                        (event_uid("Seed", title, date),)).fetchone()


class TestExtraction:
    def test_prompt_offers_the_towns_live_slugs(self, snapshot):
        prompt = build_extraction_prompt({"name": "SLT"}, "Seed", "https://x", "md")
        assert '"categories"' in prompt and '"tags"' in prompt
        for c in SLT_CATEGORIES:
            assert c["slug"] in prompt
        assert "music, arts, community, food" not in prompt

    def test_upsert_stores_normalised_categories_and_tags(self, conn, snapshot):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                    "categories": ["music", "outdoors", "sports"],
                                    "tags": ["Free", "all ages"]})
        row = _row(conn)
        assert json.loads(row["category_slugs"]) == ["live-music", "outdoors"]
        assert row["category_slug"] == "live-music"
        assert json.loads(row["tags"]) == ["free", "all-ages"]

    def test_legacy_single_category_key_is_accepted(self, conn, snapshot):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                    "category": "food"})
        assert json.loads(_row(conn)["category_slugs"]) == ["food-drink"]

    def test_no_categories_stores_null(self, conn, snapshot):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00"})
        row = _row(conn)
        assert row["category_slugs"] is None and row["category_slug"] is None and row["tags"] is None

    def test_classification_alone_is_not_a_source_change(self, conn, snapshot):
        ev = {"title": "Jazz Night", "date": "2026-10-09T20:00:00", "categories": ["music"],
              "tags": ["free"]}
        upsert_event(conn, "Seed", ev)
        assert upsert_event(conn, "Seed", {**ev, "categories": ["outdoors"],
                                           "tags": ["sunset"]}) == "unchanged"

    def test_unchanged_row_gains_categories_and_tags_it_lacked(self, conn, snapshot):
        ev = {"title": "Jazz Night", "date": "2026-10-09T20:00:00"}
        upsert_event(conn, "Seed", ev)
        upsert_event(conn, "Seed", {**ev, "categories": ["music"], "tags": ["free"]})
        row = _row(conn)
        assert json.loads(row["category_slugs"]) == ["live-music"]
        assert json.loads(row["tags"]) == ["free"]

    def test_row_hashed_before_this_change_stays_unchanged(self, conn, snapshot):
        """Rows hashed with the old formula (which included the single
        extraction `category`) must not all read as source changes — event-
        reconcile would reopen live events that were only edited on TownLoops."""
        ev = {"title": "Jazz Night", "date": "2026-10-09T20:00:00"}
        upsert_event(conn, "Seed", ev)
        old = json.dumps({"title": "Jazz Night", "date": "2026-10-09T20:00:00", "end_at": "",
                          "venue": "", "location": "", "description": "", "category": "music"},
                         sort_keys=True)
        conn.execute("UPDATE events SET content_hash = ?, category_slug = 'music'",
                     (hashlib.sha1(old.encode()).hexdigest(),))
        assert upsert_event(conn, "Seed", {**ev, "categories": ["live-music"]}) == "unchanged"
        assert _row(conn)["content_hash"] == content_hash(ev)


# --------------------------------------------------------------------------
# migration 017
# --------------------------------------------------------------------------

def test_migration_backfills_single_category(tmp_path, monkeypatch):
    from engine import db as db_mod
    real = db_mod.MIGRATIONS_DIR
    before = tmp_path / "before"
    before.mkdir()
    for p in sorted(real.glob("*.sql")):
        if p.name < "017":
            shutil.copy(p, before / p.name)
    conn = db_mod.connect(str(tmp_path / "t.db"))
    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", before)
    db_mod.run_migrations(conn)
    for uid, cat in (("a", "outdoors"), ("b", None), ("c", " ")):
        conn.execute("INSERT INTO events (event_uid, source, source_kind, name, date, date_day, "
                     "  first_seen_at, last_seen_at, content_hash, last_modified_at, category_slug) "
                     "VALUES (?, 's', 'scraped', 'n', 'd', 'd', 't', 't', 'h', 't', ?)", (uid, cat))
    conn.commit()
    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", real)
    assert any(f.startswith("017") for f in db_mod.run_migrations(conn))
    got = {r["event_uid"]: (r["category_slugs"], r["tags"])
           for r in conn.execute("SELECT event_uid, category_slugs, tags FROM events")}
    assert got == {"a": ('["outdoors"]', None), "b": (None, None), "c": (None, None)}


# --------------------------------------------------------------------------
# event-ship
# --------------------------------------------------------------------------

def _load_ship():
    path = Path(__file__).resolve().parent.parent / "skills/business/event-ship/ship.py"
    spec = importlib.util.spec_from_file_location("event_ship_cats", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def ship(conn, monkeypatch, api, snapshot):
    mod = _load_ship()
    monkeypatch.setattr(mod, "open_db", lambda: conn)
    for k, v in (("TOWNLOOPS_API_URL", "https://tl.example"), ("TOWNLOOPS_API_KEY", "k"),
                 ("TOWNLOOPS_TOWN_SLUG", "testtown")):
        monkeypatch.setenv(k, v)
    import engine.townloops_api as tl
    monkeypatch.setattr(tl, "Client", lambda: api)
    return mod


def _ship(ship, monkeypatch, conn, ev):
    upsert_event(conn, "Seed", ev)
    uid = event_uid("Seed", ev["title"], ev["date"])
    monkeypatch.setattr(ship, "parse_event_uid_arg", lambda: uid)
    return uid, ship.main()


EV = {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
      "categories": ["music", "outdoors"], "tags": ["Free", "all ages"]}


class TestShip:
    def test_payload_sends_category_slugs_and_tags(self, ship, monkeypatch, conn, api):
        _, rc = _ship(ship, monkeypatch, conn, EV)
        assert rc == 0
        [payload] = api.creates()
        assert payload["category_slugs"] == ["live-music", "outdoors"]
        assert payload["tags"] == ["free", "all-ages"]
        assert "category_slug" not in payload
        assert api.patches() == []

    def test_stale_vocabulary_on_old_rows_is_normalised_at_ship(self, ship, monkeypatch, conn, api):
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00"})
        conn.execute("UPDATE events SET category_slug = 'music', "
                     "  category_slugs = '[\"music\", \"sports\"]'")
        uid = event_uid("Seed", "Jazz Night", "2026-10-09T20:00:00")
        monkeypatch.setattr(ship, "parse_event_uid_arg", lambda: uid)
        assert ship.main() == 0
        assert api.creates()[0]["category_slugs"] == ["live-music"]

    def test_tags_dropped_on_create_are_patched_in_and_verified(
            self, ship, monkeypatch, conn):
        api = FakeAPI(create_drops=("tags",))
        import engine.townloops_api as tl
        monkeypatch.setattr(tl, "Client", lambda: api)
        _, rc = _ship(ship, monkeypatch, conn, EV)
        assert rc == 0
        [patch] = api.patches()
        assert patch["tags"] == ["free", "all-ages"]
        assert patch["category_slugs"] == ["live-music", "outdoors"]
        assert patch["title"] == "Jazz Night"  # full body, not a partial
        [created] = api.events.values()
        assert created["tags"] == ["all-ages", "free"]
        assert api.calls[-1][0] == "GET"

    def test_tags_that_never_stick_park_the_event(self, ship, monkeypatch, conn):
        api = FakeAPI(create_drops=("tags",), ignore=("tags",))
        import engine.townloops_api as tl
        monkeypatch.setattr(tl, "Client", lambda: api)
        uid, rc = _ship(ship, monkeypatch, conn, EV)
        assert rc != 0
        row = conn.execute("SELECT state, townloops_slug FROM events WHERE event_uid = ?",
                           (uid,)).fetchone()
        assert row["state"].startswith("stuck-")
        assert row["townloops_slug"] == next(iter(api.events))
        flagged = conn.execute(
            "SELECT error FROM agent_activity WHERE worker = 'event-ship' "
            "AND action = 'verify-live-event' AND ok = 0").fetchone()
        assert "tags" in flagged["error"]

    def test_event_without_classification_sends_neither(self, ship, monkeypatch, conn, api):
        _, rc = _ship(ship, monkeypatch, conn, {"title": "Jazz Night",
                                                "date": "2026-10-09T20:00:00"})
        assert rc == 0
        [payload] = api.creates()
        assert "category_slugs" not in payload and "tags" not in payload
        assert api.patches() == []

    def test_requeued_event_already_live_gets_its_dropped_tags_repaired(
            self, ship, monkeypatch, conn):
        """A retry after a failed repair 409s on create — the live row's sets
        are still checked and repaired."""
        api = FakeAPI(create_drops=("tags",), ignore=("tags",))
        import engine.townloops_api as tl
        monkeypatch.setattr(tl, "Client", lambda: api)
        uid, rc = _ship(ship, monkeypatch, conn, EV)
        assert rc != 0
        api.ignore.clear()

        def conflict(*a, **k):
            raise RuntimeError("[409 slug_taken] slug taken")
        api.create = conflict
        assert ship.main() == 0
        [created] = api.events.values()
        assert created["tags"] == ["all-ages", "free"]

    def test_missing_snapshot_ships_without_categories_and_says_so(
            self, ship, monkeypatch, conn, api, snapshot, capsys):
        upsert_event(conn, "Seed", EV)
        snapshot.unlink()
        uid = event_uid("Seed", EV["title"], EV["date"])
        monkeypatch.setattr(ship, "parse_event_uid_arg", lambda: uid)
        assert ship.main() == 0
        assert "category_slugs" not in api.creates()[0]
        assert "no town categories snapshot" in capsys.readouterr().err
