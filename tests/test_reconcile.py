"""Weekly reconcile: live cross-reference classification + local staging/enrichment."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dateutil import tz

from engine import reconcile as rc


TZ = tz.gettz("America/Los_Angeles")
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
SEED = "https://seed.example/calendar"

VENUES = [
    {"id": 69, "slug": "gunbarrel-tavern", "name": "Gunbarrel Tavern"},
    {"id": 99, "slug": "tipsy-putt", "name": "Tipsy Putt"},
]


def live_event(slug, title, start, venue_slug=None, url=None):
    return {"id": hash(slug) % 10000, "slug": slug, "title": title, "start_at": start,
            "venue_slug": venue_slug, "source_url": url}


def index(*events):
    return rc.LiveIndex.build(events, VENUES, TZ)


def classify(extracted, idx):
    return rc.classify_all(extracted, idx, seed_url=SEED, now=NOW)


class TestClassify:
    def test_unchanged_by_source_url(self):
        idx = index(live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern",
                               "https://x.example/jazz"))
        [c] = classify([{"title": "Jazz Nite!", "date": "2026-10-09T20:00:00",
                         "venue": "Gunbarrel Tavern", "source_url": "https://www.x.example/jazz/"}], idx)
        assert c.kind == "unchanged" and c.matched_by == "source_url"

    def test_new_when_no_match(self):
        idx = index(live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z"))
        [c] = classify([{"title": "Pumpkin Regatta", "date": "2026-10-12T10:00:00"}], idx)
        assert c.kind == "new"

    def test_date_change_single_occurrence(self):
        idx = index(live_event("fest", "Fall Harvest Festival", "2026-10-17T17:00:00Z"))
        [c] = classify([{"title": "Fall Harvest Festival", "date": "2026-10-24T10:00:00"}], idx)
        assert c.kind == "changed"
        assert c.changes["start_at"]["source_local"].startswith("2026-10-24T10:00")
        assert c.live["slug"] == "fest"

    def test_time_change_same_day(self):
        idx = index(live_event("fest", "Fall Harvest Festival", "2026-10-17T17:00:00Z"))  # 10:00 PT
        [c] = classify([{"title": "Fall Harvest Festival", "date": "2026-10-17T13:00:00"}], idx)
        assert c.kind == "changed" and "start_at" in c.changes

    def test_small_time_drift_ignored(self):
        idx = index(live_event("fest", "Fall Harvest Festival", "2026-10-17T17:00:00Z"))
        [c] = classify([{"title": "Fall Harvest Festival", "date": "2026-10-17T10:15:00"}], idx)
        assert c.kind == "unchanged"

    def test_date_only_compares_day(self):
        idx = index(live_event("fest", "Fall Harvest Festival", "2026-10-17T17:00:00Z"))
        [c] = classify([{"title": "Fall Harvest Festival", "date": "2026-10-17"}], idx)
        assert c.kind == "unchanged"

    def test_venue_change(self):
        idx = index(live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern"))
        [c] = classify([{"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                         "venue": "Tipsy Putt"}], idx)
        assert c.kind == "changed"
        assert c.changes["venue"] == {"live": "Gunbarrel Tavern", "live_slug": "gunbarrel-tavern",
                                      "source": "Tipsy Putt"}

    def test_venue_name_variant_not_a_change(self):
        idx = index(live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern"))
        [c] = classify([{"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                         "venue": "The Gunbarrel Tavern & Tap House"}], idx)
        assert c.kind == "unchanged"

    def test_recurring_series_new_occurrence_not_date_change(self):
        idx = index(
            live_event("music-1", "Daily Live Music", "2026-10-07T19:00:00Z"),
            live_event("music-2", "Daily Live Music", "2026-10-08T19:00:00Z"),
        )
        out = classify([
            {"title": "Daily Live Music", "date": "2026-10-07T12:00:00"},
            {"title": "Daily Live Music", "date": "2026-10-08T12:00:00"},
            {"title": "Daily Live Music", "date": "2026-10-09T12:00:00"},
        ], idx)
        assert [c.kind for c in out] == ["unchanged", "unchanged", "new"]

    def test_same_day_claim_beats_date_change(self):
        # Two live occurrences; source drops one and keeps the other. The kept
        # one is unchanged; the dropped one must not be "moved" onto a new date
        # that already belongs to another occurrence.
        idx = index(
            live_event("trivia-1", "Trivia Night", "2026-10-08T02:00:00Z"),
            live_event("trivia-2", "Trivia Night", "2026-10-15T02:00:00Z"),
        )
        out = classify([{"title": "Trivia Night", "date": "2026-10-14T19:00:00"}], idx)
        assert out[0].kind == "unchanged" and out[0].live["slug"] == "trivia-2"

    def test_seed_listing_url_is_not_a_key(self):
        idx = index(live_event("a", "Something Else", "2026-10-10T03:00:00Z", url=SEED))
        [c] = classify([{"title": "Pottery Class", "date": "2026-10-09T20:00:00",
                         "source_url": SEED}], idx)
        assert c.kind == "new"

    def test_past_and_unparseable_skipped(self):
        out = classify([{"title": "Old", "date": "2026-09-01T10:00:00"},
                        {"title": "Bad", "date": "next tuesday-ish"}], index())
        assert [c.kind for c in out] == ["skipped", "skipped"]


class TestNameMatch:
    @pytest.mark.parametrize("a,b,ok", [
        ("AleWorX Stateline", "Aleworx Stateline", True),
        ("Gunbarrel", "Gunbarrel Tavern", True),
        ("Tahoe", "Tahoe Library", False),
        ("Lake Tahoe Community College", "Lake Tahoe Community Church", False),
    ])
    def test_names_match(self, a, b, ok):
        assert rc.names_match(a, b) is ok


# --------------------------------------------------------------------------
# Skill: staging + enrichment against a fresh DB, network stubbed out.
# --------------------------------------------------------------------------

def _load_skill():
    path = Path(__file__).resolve().parent.parent / "skills/business/event-reconcile/reconcile.py"
    spec = importlib.util.spec_from_file_location("event_reconcile", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeLive:
    def __init__(self, events=(), venues=(), businesses=(), communities=()):
        self.events = list(events)
        self.entities = {"venue": list(venues), "business": list(businesses),
                         "community": list(communities)}
        self.index = rc.LiveIndex.build(self.events, self.entities["venue"], TZ)


@pytest.fixture
def skill(fresh_db, monkeypatch):
    import sqlite3
    mod = _load_skill()
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    monkeypatch.setattr(mod, "open_db", lambda: fresh_db)
    research_calls = []

    class Res:
        def __init__(self, url):
            self.payload = {"image_path": f"{url}/og.png", "description": "From site",
                            "phone": "555-0100"}
            self.fetched_from = [url]

    def fake_research(url, use_firecrawl=False):
        research_calls.append(url)
        return Res(url)

    import engine.research as research
    import engine.discovery as discovery
    monkeypatch.setattr(research, "research_url", fake_research)
    monkeypatch.setattr(research, "verify_image", lambda u: True)
    monkeypatch.setattr(discovery, "firecrawl_search",
                        lambda q, limit=5: [{"url": "https://pumpkinpatch.example", "title": "Pumpkin Patch Co"}])
    mod.research_calls = research_calls
    return mod, fresh_db


def _classified(live, extracted):
    return rc.classify_all(extracted, live.index, seed_url=SEED, now=NOW)


class TestStageAndEnrich:
    def test_new_event_staged_and_entities_created(self, skill):
        mod, conn = skill
        live = FakeLive(venues=VENUES)
        [c] = _classified(live, [{"title": "Pumpkin Carving", "date": "2026-10-20T15:00:00",
                                  "venue": "Pumpkin Patch Co", "organizer": "Pumpkin Patch Co",
                                  "organizer_kind": "business",
                                  "source_url": "https://pumpkinpatch.example/carving"}])
        uid, action = mod._stage(conn, "Seed", c, "run-1")
        assert action == "inserted"
        row = conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone()
        assert row["approval_status"] == "pending" and row["state"] == "metadata-extracted"
        assert row["date_utc"] == "2026-10-20T22:00:00+00:00"
        assert json.loads(row["extra_json"])["reconcile"]["kind"] == "new"

        report = mod.enrich(conn, live, {"name": "South Lake Tahoe"}, [uid], max_lookups=20)
        row = conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone()
        assert row["image_url"] == "https://pumpkinpatch.example/carving/og.png"
        venue = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (row["derived_venue_uid"],)).fetchone()
        assert venue["name"] == "Pumpkin Patch Co" and venue["image_path"]
        assert venue["enrichment_status"] == "enriched" and venue["phone"] == "555-0100"
        assert row["derived_host_kind"] == "business"
        biz = conn.execute("SELECT * FROM businesses WHERE entity_uid = ?", (row["derived_host_uid"],)).fetchone()
        assert biz["image_path"] and biz["website"] == "https://pumpkinpatch.example"
        assert len(report["created"]["venue"]) == 1

    def test_changed_event_mirrors_live_venue_and_sets_slug(self, skill):
        mod, conn = skill
        live = FakeLive(
            events=[live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern")],
            venues=[{**VENUES[1], "image_path": "/static/tipsy.png"}, VENUES[0]],
        )
        [c] = _classified(live, [{"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                  "venue": "Tipsy Putt"}])
        assert c.kind == "changed"
        uid, _ = mod._stage(conn, "Seed", c, "run-1")
        mod.enrich(conn, live, {}, [uid], max_lookups=5)
        row = conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone()
        assert row["townloops_slug"] == "jazz"
        assert "changed" in row["approval_note"]
        v = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (row["derived_venue_uid"],)).fetchone()
        assert v["source_kind"] == "live" and v["townloops_slug"] == "tipsy-putt"
        assert json.loads(v["extra_json"])["live_id"] == 99
        assert v["image_path"] == "/static/tipsy.png"  # live image kept, no research needed
        assert mod.research_calls == []

    def test_live_entity_without_image_gets_proposal(self, skill):
        mod, conn = skill
        live = FakeLive(communities=[{"id": 5, "slug": "rotary", "name": "Rotary Club of Tahoe",
                                      "website": "https://rotary.example", "image_path": None}])
        [c] = _classified(live, [{"title": "Rotary Pancake Breakfast", "date": "2026-10-20T08:00:00",
                                  "organizer": "Rotary Club of Tahoe", "organizer_kind": "community"}])
        uid, _ = mod._stage(conn, "Seed", c, "run-1")
        report = mod.enrich(conn, live, {}, [uid], max_lookups=5)
        assert report["live_image_proposals"] == [
            {"kind": "community", "slug": "rotary", "image_path": "https://rotary.example/og.png"}]
        row = conn.execute("SELECT c.* FROM communities c JOIN events e ON e.derived_host_uid = c.entity_uid "
                           "WHERE e.event_uid = ?", (uid,)).fetchone()
        assert row["enrichment_status"] == "image-proposed"

    def test_unchanged_flags_pending_duplicate(self, skill):
        mod, conn = skill
        from engine.event_extract import upsert_event
        ev = {"title": "Jazz Night", "date": "2026-10-09T20:00:00", "venue": "Gunbarrel Tavern"}
        upsert_event(conn, "Seed", ev)  # what the daily event-find would have done
        live = FakeLive(events=[live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern")],
                        venues=VENUES)
        [c] = _classified(live, [ev])
        assert c.kind == "unchanged"
        assert mod._annotate_already_live(conn, "Seed", c)
        note = conn.execute("SELECT approval_note FROM events").fetchone()[0]
        assert "already live as jazz" in note

    def test_budget_caps_lookups(self, skill):
        mod, conn = skill
        live = FakeLive()
        uids = []
        for i in range(5):
            [c] = _classified(live, [{"title": f"Event {i}", "date": "2026-10-20T08:00:00",
                                      "venue": f"Venue Number {i}", "source_url": f"https://e.example/{i}"}])
            uids.append(mod._stage(conn, "Seed", c, "run-1")[0])
        report = mod.enrich(conn, live, {}, uids, max_lookups=3)
        assert len(mod.research_calls) <= 3 and report["lookups_left"] == 0


# --------------------------------------------------------------------------
# Downstream: event-ship PATCHes changed live events; derive honors presets.
# --------------------------------------------------------------------------

def _load(rel, name):
    path = Path(__file__).resolve().parent.parent / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    def __init__(self):
        self.calls = []

    def update(self, resource, slug, payload):
        self.calls.append(("PATCH", resource, slug, payload))
        return {"slug": slug}

    def create(self, resource, payload, idempotency_key=None):
        self.calls.append(("POST", resource, payload.get("slug"), payload))
        return {"slug": payload.get("slug")}


class TestDownstream:
    def _staged_change(self, skill):
        mod, conn = skill
        live = FakeLive(
            events=[live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern")],
            venues=[{**VENUES[1], "image_path": "/x.png"}, VENUES[0]],
        )
        [c] = _classified(live, [{"title": "Jazz Night", "date": "2026-10-16T20:00:00",
                                  "venue": "Tipsy Putt"}])
        uid, _ = mod._stage(conn, "Seed", c, "run-1")
        mod.enrich(conn, live, {}, [uid], max_lookups=0)
        return conn, uid

    def test_ship_patches_live_event(self, skill):
        conn, uid = self._staged_change(skill)
        ship = _load("skills/business/event-ship/ship.py", "event_ship")
        event = dict(conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())
        client = FakeClient()
        slug = ship._patch_live_event(client, conn, event, ship._reconcile_change(event))
        assert slug == "jazz"
        [(verb, res, s, payload)] = client.calls
        assert (verb, res, s) == ("PATCH", "events", "jazz")
        assert payload["start_at"] == "2026-10-17T03:00:00+00:00"
        assert payload["venue_name"] == "Tipsy Putt" and payload["venue_id"] == 99

    def test_ship_skips_post_for_live_mirror(self, skill):
        conn, uid = self._staged_change(skill)
        ship = _load("skills/business/event-ship/ship.py", "event_ship")
        vuid = conn.execute("SELECT derived_venue_uid FROM events WHERE event_uid = ?", (uid,)).fetchone()[0]
        client = FakeClient()
        assert ship._ship_entity(client, conn, "venue", vuid) == "tipsy-putt"
        assert client.calls == []

    def test_derive_venue_honors_preset(self, skill, monkeypatch):
        conn, uid = self._staged_change(skill)
        derive = _load("skills/business/event-derive-venue/derive.py", "derive_venue")
        monkeypatch.setattr(derive, "open_db", lambda: conn)
        monkeypatch.setattr(derive, "parse_event_uid_arg", lambda: uid)
        before = conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0]
        assert derive.main() == 0
        assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == before
        link = conn.execute("SELECT entity_uid FROM linked_events WHERE event_uid = ?", (uid,)).fetchone()
        assert link[0] == conn.execute("SELECT derived_venue_uid FROM events WHERE event_uid = ?", (uid,)).fetchone()[0]
