"""Weekly reconcile: live cross-reference classification + local staging/enrichment."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dateutil import tz

from engine import reconcile as rc
from tests.fake_townloops import FakeAPI


TZ = tz.gettz("America/Los_Angeles")
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
SEED = "https://seed.example/calendar"

VENUES = [
    {"id": 69, "slug": "gunbarrel-tavern", "name": "Gunbarrel Tavern"},
    {"id": 99, "slug": "tipsy-putt", "name": "Tipsy Putt"},
]


def live_event(slug, title, start, venue_slug=None, url=None, end=None):
    return {"id": hash(slug) % 10000, "slug": slug, "title": title, "start_at": start,
            "end_at": end, "venue_slug": venue_slug, "source_url": url}


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

    def test_past_series_occurrence_not_moved_onto_new_date(self):
        # Weekly series: yesterday's occurrence is still in the live window but
        # never on the source page, so it is the only unclaimed candidate.
        idx = index(
            live_event("music-0", "Live Music", "2026-10-05T02:00:00Z"),
            live_event("music-1", "Live Music", "2026-10-12T02:00:00Z"),
            live_event("music-2", "Live Music", "2026-10-19T02:00:00Z"),
        )
        out = classify([
            {"title": "Live Music", "date": "2026-10-11T19:00:00"},
            {"title": "Live Music", "date": "2026-10-18T19:00:00"},
            {"title": "Live Music", "date": "2026-10-25T19:00:00"},
        ], idx)
        assert [c.kind for c in out] == ["unchanged", "unchanged", "new"]

    def test_series_dropped_occurrence_not_moved(self):
        # Source drops Oct 18 and lists Oct 25: that is a new occurrence, not
        # the Oct 18 live row rescheduled.
        idx = index(
            live_event("music-1", "Live Music", "2026-10-12T02:00:00Z"),
            live_event("music-2", "Live Music", "2026-10-19T02:00:00Z"),
        )
        out = classify([
            {"title": "Live Music", "date": "2026-10-11T19:00:00"},
            {"title": "Live Music", "date": "2026-10-25T19:00:00"},
        ], idx)
        assert [c.kind for c in out] == ["unchanged", "new"]

    def test_past_single_occurrence_not_rescheduled(self):
        idx = index(live_event("fest", "Fall Harvest Festival", "2026-10-05T17:00:00Z"))
        [c] = classify([{"title": "Fall Harvest Festival", "date": "2026-10-24T10:00:00"}], idx)
        assert c.kind == "new"

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
            self.text = "Pumpkin Patch Co\nThe growers' guild meets every Monday at 6pm in the barn."

    def fake_research(url, use_firecrawl=False):
        research_calls.append(url)
        return Res(url)

    import engine.research as research
    import engine.discovery as discovery
    monkeypatch.setattr(research, "research_url", fake_research)
    monkeypatch.setattr(research, "verify_image", lambda u: True)
    monkeypatch.setattr(discovery, "firecrawl_search",
                        lambda q, limit=5, **kw: [{"url": "https://pumpkinpatch.example", "title": "Pumpkin Patch Co"}])
    mod.research_calls = research_calls
    return mod, fresh_db


def _classified(live, extracted):
    return rc.classify_all(extracted, live.index, seed_url=SEED, now=NOW)


class TestStageAndEnrich:
    def test_new_event_staged_and_entities_created(self, skill):
        mod, conn = skill
        live = FakeLive(venues=VENUES)
        [c] = _classified(live, [{"title": "Pumpkin Carving", "date": "2026-10-20T15:00:00",
                                  "venue": "Pumpkin Patch Co", "organizer": "Harvest Growers LLC",
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
        assert biz["name"] == "Harvest Growers LLC"  # not the venue: a host of its own
        assert len(report["created"]["venue"]) == 1

    def test_organizer_that_is_the_venue_gets_no_host(self, skill):
        """Issue #4: "Plates at Cascade Kitchens" shipped a business for its
        organizer although live venue cascade-kitchens is the venue."""
        mod, conn = skill
        cascade = {"id": 41, "slug": "cascade-kitchens", "name": "Cascade Kitchens"}
        live = FakeLive(venues=[*VENUES, cascade])
        [c] = _classified(live, [{"title": "Plates at Cascade Kitchens", "date": "2026-10-20T18:00:00",
                                  "venue": "Cascade Kitchens",
                                  "organizer": "Cascade Kitchens Food Hall & Bar",
                                  "organizer_kind": "business"}])
        uid, _ = mod._stage(conn, "Seed", c, "run-1")
        mod.enrich(conn, live, {}, [uid], max_lookups=5)
        row = conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone()
        v = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (row["derived_venue_uid"],)).fetchone()
        assert v["townloops_slug"] == "cascade-kitchens"
        assert row["derived_host_uid"] is None and row["derived_host_kind"] is None
        assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 0
        [detail] = [r[0] for r in conn.execute(
            "SELECT detail FROM agent_activity WHERE action = 'host-is-venue'")]
        assert "Cascade Kitchens" in detail

    def test_organizer_that_is_another_live_venue_is_kept_and_logged(self, skill):
        mod, conn = skill
        cascade = {"id": 41, "slug": "cascade-kitchens", "name": "Cascade Kitchens"}
        live = FakeLive(venues=[*VENUES, cascade])
        [c] = _classified(live, [{"title": "Chef Pop-up", "date": "2026-10-20T18:00:00",
                                  "venue": "Tipsy Putt",
                                  "organizer": "Cascade Kitchens Food Hall & Bar",
                                  "organizer_kind": "business"}])
        uid, _ = mod._stage(conn, "Seed", c, "run-1")
        mod.enrich(conn, live, {}, [uid], max_lookups=5)
        row = conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone()
        assert row["derived_host_kind"] == "business"
        assert conn.execute("SELECT COUNT(*) FROM agent_activity WHERE action = 'host-is-venue'"
                            ).fetchone()[0] == 0
        [detail] = [r[0] for r in conn.execute(
            "SELECT detail FROM agent_activity WHERE action = 'host-matches-other-venue'")]
        assert "cascade-kitchens" in detail

    def test_new_community_host_gets_meeting_info_from_its_site(self, skill):
        mod, conn = skill
        live = FakeLive(venues=VENUES)
        [c] = _classified(live, [{"title": "Harvest Social", "date": "2026-10-21T15:00:00",
                                  "organizer": "Pumpkin Patch Co", "organizer_kind": "community",
                                  "source_url": "https://pumpkinpatch.example/social"}])
        uid, _ = mod._stage(conn, "Seed", c, "run-1")
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            return json.dumps({"meeting_info": [
                "The growers' guild meets every Monday at 6pm in the barn."]})
        mod.enrich(conn, live, {}, [uid], max_lookups=5, llm=llm)
        row = conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone()
        assert row["derived_host_kind"] == "community"
        com = conn.execute("SELECT * FROM communities WHERE entity_uid = ?",
                           (row["derived_host_uid"],)).fetchone()
        assert com["meeting_info"] == "The growers' guild meets every Monday at 6pm in the barn."
        assert len(prompts) == 1

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
        assert row["townloops_slug"] is None  # set by event-ship after the PATCH
        assert json.loads(row["extra_json"])["reconcile"]["live_slug"] == "jazz"
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

    def test_restaged_as_new_clears_stale_reconcile_slug(self, skill):
        mod, conn = skill
        ev = {"title": "Jazz Night", "date": "2026-10-16T20:00:00", "venue": "Tipsy Putt"}
        live = FakeLive(events=[live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z")], venues=VENUES)
        [c] = _classified(live, [ev])
        uid, _ = mod._stage(conn, "Seed", c, "run-1")
        # A row staged by an older reconcile that wrote the live slug locally.
        conn.execute("UPDATE events SET townloops_slug = 'jazz' WHERE event_uid = ?", (uid,))
        [c2] = _classified(FakeLive(venues=VENUES), [ev])  # live event gone
        assert c2.kind == "new"
        mod._stage(conn, "Seed", c2, "run-2")
        row = conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone()
        assert row["townloops_slug"] is None

    def test_change_to_shipped_row_goes_back_to_review(self, skill):
        mod, conn = skill
        from engine.derive_helpers import link_event_to_entity
        from engine.event_extract import upsert_event
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                     "venue": "Gunbarrel Tavern"})
        uid = conn.execute("SELECT event_uid FROM events").fetchone()[0]
        conn.execute("UPDATE events SET state = 'shipped', approval_status = 'approved', "
                     "derived_venue_uid = 'old-venue', townloops_slug = 'jazz', pushed_at = 'x' "
                     "WHERE event_uid = ?", (uid,))
        link_event_to_entity(conn, uid, "venue", "old-venue", role="primary")
        live = FakeLive(events=[live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern")],
                        venues=VENUES)
        [c] = _classified(live, [{"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                  "venue": "Tipsy Putt"}])
        assert c.kind == "changed"
        assert mod._stage(conn, "Seed", c, "run-1")[0] == uid
        row = conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone()
        assert row["state"] == "metadata-extracted" and row["approval_status"] == "pending"
        assert row["derived_venue_uid"] is None
        assert "changed" in row["approval_note"]
        assert conn.execute("SELECT COUNT(*) FROM linked_events WHERE event_uid = ? "
                            "AND entity_kind = 'venue'", (uid,)).fetchone()[0] == 0

    def test_live_only_difference_does_not_reopen_shipped_row(self, skill):
        # Source unchanged since we shipped; the live row was edited on
        # TownLoops. Reopening would PATCH the human edit away.
        mod, conn = skill
        from engine.event_extract import upsert_event
        ev = {"title": "Jazz Night", "date": "2026-10-09T20:00:00", "venue": "Tipsy Putt"}
        upsert_event(conn, "Seed", ev)
        conn.execute("UPDATE events SET state = 'shipped', approval_status = 'approved'")
        live = FakeLive(events=[live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern")],
                        venues=VENUES)
        [c] = _classified(live, [ev])
        assert c.kind == "changed"
        mod._stage(conn, "Seed", c, "run-1")
        assert conn.execute("SELECT state FROM events").fetchone()[0] == "shipped"

    def test_reopened_stuck_row_gets_fresh_workqueue(self, skill):
        mod, conn = skill
        from engine.event_extract import upsert_event
        from engine import workqueue
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                     "venue": "Gunbarrel Tavern"})
        uid = conn.execute("SELECT event_uid FROM events").fetchone()[0]
        conn.execute("UPDATE events SET state = 'stuck-entities-enriched', approval_status = 'approved'")
        workqueue.enqueue(conn, uid, "ship")
        conn.execute("UPDATE event_workqueue SET attempts = 3, resolved_at = 'x'")
        live = FakeLive(events=[live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern")],
                        venues=VENUES)
        [c] = _classified(live, [{"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                  "venue": "Tipsy Putt"}])
        mod._stage(conn, "Seed", c, "run-1")
        assert conn.execute("SELECT state FROM events").fetchone()[0] == "metadata-extracted"
        assert conn.execute("SELECT COUNT(*) FROM event_workqueue WHERE event_uid = ? AND attempts > 0",
                            (uid,)).fetchone()[0] == 0

    def test_restage_drops_end_the_source_no_longer_gives(self, skill):
        mod, conn = skill
        live = FakeLive(events=[live_event("fest", "Fall Fest", "2026-10-10T03:00:00Z")])
        [c] = _classified(live, [{"title": "Fall Fest", "date": "2026-10-16T19:00:00",
                                  "end_at": "2026-10-17T02:00:00"}])
        uid, _ = mod._stage(conn, "Seed", c, "run-1")
        [c2] = _classified(live, [{"title": "Fall Fest", "date": "2026-10-16T19:00:00"}])
        mod._stage(conn, "Seed", c2, "run-2")
        assert conn.execute("SELECT end_at_utc FROM events WHERE event_uid = ?", (uid,)).fetchone()[0] is None

    def test_change_to_declined_row_left_alone(self, skill):
        mod, conn = skill
        from engine.event_extract import upsert_event
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                     "venue": "Gunbarrel Tavern"})
        conn.execute("UPDATE events SET state = 'declined', approval_status = 'declined'")
        live = FakeLive(events=[live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern")],
                        venues=VENUES)
        [c] = _classified(live, [{"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                  "venue": "Tipsy Putt"}])
        mod._stage(conn, "Seed", c, "run-1")
        assert conn.execute("SELECT state FROM events").fetchone()[0] == "declined"

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

    def test_venue_cut_off_by_budget_is_researched_next_week(self, skill, tmp_path, monkeypatch):
        mod, conn = skill
        live = FakeLive()
        [c] = _classified(live, [{"title": "Pumpkin Carving", "date": "2026-10-20T15:00:00",
                                  "venue": "Pumpkin Patch Co"}])
        uid, _ = mod._stage(conn, "Seed", c, "run-1")
        report = mod.enrich(conn, live, {}, [uid], max_lookups=0)
        vuid = conn.execute("SELECT derived_venue_uid FROM events").fetchone()[0]
        status = lambda: conn.execute("SELECT enrichment_status FROM venues WHERE entity_uid = ?",
                                      (vuid,)).fetchone()[0]
        assert status() == "pending"
        monkeypatch.setattr(mod, "_REPORT_DIR", tmp_path)
        text = mod._write_report("2026-10-11T02:00:00", None, report).read_text()
        assert "New venue: Pumpkin Patch Co (research deferred to next run — lookup budget ran out or site unreadable)" in text

        conn.execute("UPDATE events SET derived_venue_uid = NULL")
        mod.enrich(conn, live, {}, [uid], max_lookups=5)
        assert conn.execute("SELECT derived_venue_uid FROM events").fetchone()[0] == vuid
        assert status() == "enriched"


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
    def __init__(self, live=None):
        self.calls = []
        self.live = live or {}

    def get(self, resource, slug):
        self.calls.append(("GET", resource, slug, None))
        return self.live[slug]

    def update(self, resource, slug, payload):
        self.calls.append(("PATCH", resource, slug, payload))
        return {"slug": slug}

    def create(self, resource, payload, idempotency_key=None):
        self.calls.append(("POST", resource, payload.get("slug"), payload))
        return {"slug": payload.get("slug")}


class TestDownstream:
    def _staged_change(self, skill, venue="Tipsy Putt", end=None, src_end=None):
        mod, conn = skill
        live = FakeLive(
            events=[live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern", end=end)],
            venues=[{**VENUES[1], "image_path": "/x.png"}, VENUES[0]],
        )
        ev = {"title": "Jazz Night", "date": "2026-10-16T20:00:00", "venue": venue}
        if src_end:
            ev["end_at"] = src_end
        [c] = _classified(live, [ev])
        uid, _ = mod._stage(conn, "Seed", c, "run-1")
        mod.enrich(conn, live, {}, [uid], max_lookups=0)
        return conn, uid

    def _api(self, end=None, ignore=()):
        row = {**live_event("jazz", "Jazz Night", "2026-10-10T03:00:00Z", "gunbarrel-tavern", end=end),
               "description": "Weekly jazz.", "venue_id": 69, "is_featured": False,
               "category_slugs": ["food-drink", "live-music"], "tags": ["jazz"]}
        venues = VENUES + [{"id": 123, "slug": "brand-new-hall", "name": "Brand New Hall"},
                           {"id": 124, "slug": "hermes-slt-abc", "name": "Brand New Hall (hermes)"}]
        return FakeAPI([row], venues, ignore=ignore)

    def _patch(self, conn, uid, venue_slug=None, client=None, end=None):
        ship = _load("skills/business/event-ship/ship.py", "event_ship")
        event = dict(conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())
        client = client or self._api(end=end)
        slug = ship._patch_live_event(client, conn, event, ship._reconcile_change(event),
                                      venue_slug=venue_slug)
        patches = client.patches()
        return (patches[0] if patches else None) if slug else None

    def test_ship_patches_live_event_with_full_body(self, skill):
        conn, uid = self._staged_change(skill)
        client = self._api()
        payload = self._patch(conn, uid, client=client)
        assert payload["start_at"] == "2026-10-17T03:00:00+00:00"
        assert payload["venue_id"] == 99
        # untouched live fields go back as they were — nothing is blanked
        assert payload["title"] == "Jazz Night"
        assert payload["description"] == "Weekly jazz."
        assert payload["category_slugs"] == ["food-drink", "live-music"]
        assert payload["tags"] == ["jazz"]
        live = client.events["jazz"]
        assert live["title"] == "Jazz Night" and live["venue_slug"] == "tipsy-putt"

    def test_ship_patch_finds_a_renamed_live_event_by_ref(self, skill):
        from engine.ingest_ref import ref_for
        conn, uid = self._staged_change(skill)
        conn.execute("UPDATE events SET townloops_slug = 'jazz', pushed_at = 'x' "
                     "WHERE event_uid = ?", (uid,))
        client = self._api()
        client.events["jazz"]["ingest_ref"] = ref_for(uid)
        client.rename("events", "jazz", "jazz-at-the-tavern")
        payload = self._patch(conn, uid, client=client)
        assert payload["start_at"] == "2026-10-17T03:00:00+00:00"
        assert client.events["jazz-at-the-tavern"]["start_at"] == "2026-10-17T03:00:00+00:00"
        row = conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone()
        assert row["townloops_slug"] == "jazz-at-the-tavern"
        assert json.loads(row["extra_json"])["reconcile"]["live_slug"] == "jazz-at-the-tavern"

    def test_ship_patch_fills_blank_live_accessibility_notes(self, skill):
        conn, uid = self._staged_change(skill)
        conn.execute("UPDATE events SET accessibility_notes = 'Wheelchair seating up front.' "
                     "WHERE event_uid = ?", (uid,))
        client = self._api()
        payload = self._patch(conn, uid, client=client)
        assert payload["accessibility_notes"] == "Wheelchair seating up front."
        assert client.events["jazz"]["accessibility_notes"] == "Wheelchair seating up front."

    def test_ship_patch_keeps_live_accessibility_notes(self, skill):
        conn, uid = self._staged_change(skill)
        conn.execute("UPDATE events SET accessibility_notes = 'Wheelchair seating up front.' "
                     "WHERE event_uid = ?", (uid,))
        client = self._api()
        client.events["jazz"]["accessibility_notes"] = "Ramp at side door."
        payload = self._patch(conn, uid, client=client)
        assert payload["accessibility_notes"] == "Ramp at side door."
        assert client.events["jazz"]["accessibility_notes"] == "Ramp at side door."

    def test_ship_patch_with_only_notes_to_fill_still_patches(self, skill):
        conn, uid = self._staged_change(skill, venue="Gunbarrel Tavern")
        meta_sql = ("UPDATE events SET accessibility_notes = 'Wheelchair seating up front.', "
                    "extra_json = json_set(extra_json, '$.reconcile.changes', json('{}')) "
                    "WHERE event_uid = ?")
        conn.execute(meta_sql, (uid,))
        client = self._api()
        payload = self._patch(conn, uid, client=client)
        assert payload["accessibility_notes"] == "Wheelchair seating up front."

    def test_ship_patch_tops_the_live_description_with_one_ticket_line(self, skill):
        conn, uid = self._staged_change(skill)
        conn.execute("UPDATE events SET price = 'Tickets $20.', "
                     "rsvp_url = 'https://tickets.example/e/jazz' WHERE event_uid = ?", (uid,))
        client = self._api()
        line = "Tickets: $20 — https://tickets.example/e/jazz"
        assert self._patch(conn, uid, client=client)["description"] == f"{line}\n\nWeekly jazz."
        self._patch(conn, uid, client=client)          # a second PATCH doesn't stack it
        assert client.events["jazz"]["description"] == f"{line}\n\nWeekly jazz."

    def test_ship_patch_with_only_ticket_info_is_idempotent(self, skill):
        conn, uid = self._staged_change(skill, venue="Gunbarrel Tavern")
        conn.execute("UPDATE events SET price = 'Free admission.', "
                     "extra_json = json_set(extra_json, '$.reconcile.changes', json('{}')) "
                     "WHERE event_uid = ?", (uid,))
        client = self._api()
        assert "free" in self._patch(conn, uid, client=client)["tags"]
        n = len(client.patches())
        self._patch(conn, uid, client=client)
        assert len(client.patches()) == n
        assert client.events["jazz"]["description"] == "Weekly jazz."

    def test_venue_change_unresolved_fails_for_retry(self, skill):
        conn, uid = self._staged_change(skill, venue="Brand New Hall")
        client = self._api()
        assert self._patch(conn, uid, venue_slug=None, client=client) is None
        assert not client.patches()

    def test_venue_slug_comes_from_derived_venue(self, skill):
        conn, uid = self._staged_change(skill, venue="Brand New Hall")
        vuid = conn.execute("SELECT derived_venue_uid FROM events WHERE event_uid = ?", (uid,)).fetchone()[0]
        conn.execute("UPDATE venues SET townloops_slug = 'brand-new-hall' WHERE entity_uid = ?", (vuid,))
        payload = self._patch(conn, uid, venue_slug="some-other-linked-venue")
        # PATCH has no venue_slug field (the API drops it) — the slug is
        # resolved to the live venue id.
        assert payload["venue_id"] == 123 and "venue_slug" not in payload

    def test_patch_supersedes_older_local_copy(self, skill):
        conn, uid = self._staged_change(skill, venue="Gunbarrel Tavern")
        from engine.event_extract import upsert_event
        upsert_event(conn, "Seed", {"title": "Jazz Night", "date": "2026-10-09T20:00:00"})
        conn.execute("UPDATE events SET state = 'shipped', townloops_slug = 'jazz' WHERE event_uid != ?", (uid,))
        ship = _load("skills/business/event-ship/ship.py", "event_ship")
        ship._supersede_older_copies(conn, uid, "jazz")
        old = conn.execute("SELECT * FROM events WHERE event_uid != ?", (uid,)).fetchone()
        assert old["townloops_slug"] is None and old["status"] == "superseded"
        assert json.loads(old["extra_json"])["superseded_by"] == uid

    def test_date_change_without_source_end_shifts_live_end(self, skill):
        # Live 20:00–22:00 PT on Oct 9; source moves it to Oct 16 20:00, no end.
        conn, uid = self._staged_change(skill, venue="Gunbarrel Tavern", end="2026-10-10T05:00:00Z")
        payload = self._patch(conn, uid, end="2026-10-10T05:00:00Z")
        assert payload["start_at"] == "2026-10-17T03:00:00+00:00"
        assert payload["end_at"] == "2026-10-17T05:00:00+00:00"

    def test_date_change_never_sends_end_before_start(self, skill):
        conn, uid = self._staged_change(skill, venue="Gunbarrel Tavern", src_end="2026-10-09T22:00:00")
        payload = self._patch(conn, uid)
        assert "end_at" not in payload or payload["end_at"] > payload["start_at"]

    def test_venue_change_attaches_shipped_venue(self, skill):
        conn, uid = self._staged_change(skill, venue="Brand New Hall")
        vuid = conn.execute("SELECT derived_venue_uid FROM events WHERE event_uid = ?", (uid,)).fetchone()[0]
        conn.execute("UPDATE venues SET townloops_slug = 'hermes-slt-abc' WHERE entity_uid = ?", (vuid,))
        client = self._api()
        payload = self._patch(conn, uid, client=client)
        assert payload["venue_id"] == 124
        assert client.events["jazz"]["venue_slug"] == "hermes-slt-abc"

    def test_verify_mismatch_flags_event_for_operator(self, skill):
        conn, uid = self._staged_change(skill, venue="Gunbarrel Tavern")
        from engine import workqueue
        conn.execute("UPDATE events SET state = 'entities-enriched' WHERE event_uid = ?", (uid,))
        workqueue.enqueue(conn, uid, "ship")
        client = self._api(ignore={"start_at"})
        assert self._patch(conn, uid, client=client) is None
        row = conn.execute("SELECT state, townloops_slug FROM events WHERE event_uid = ?", (uid,)).fetchone()
        assert row["state"] == "stuck-entities-enriched"
        assert row["townloops_slug"] is None
        open_rows = conn.execute("SELECT COUNT(*) FROM event_workqueue "
                                 "WHERE event_uid = ? AND resolved_at IS NULL", (uid,)).fetchone()[0]
        assert open_rows == 0  # no automatic re-PATCH; the operator decides
        act = conn.execute("SELECT * FROM agent_activity WHERE target = ? AND ok = 0 "
                           "ORDER BY rowid DESC LIMIT 1", (uid,)).fetchone()
        assert "start_at" in act["error"]

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
