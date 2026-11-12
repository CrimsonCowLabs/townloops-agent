"""Cross-reference against live before any create, and live mirror
enrichment (#64 stories 36-38, 41-43, 47-49): a whole town in autonomous
mode (tests/autonomy_town.py) against hand-made live rows in the fake
TownLoops — what it submits, what it binds instead, what it fills on someone
else's row, and what the daily report says about it."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.autonomy_town import PROFILE, Town, soon

ARENA = "https://arena.example/events"
TZ = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    t = Town(tmp_state_dir, monkeypatch, fresh_db)
    t.set_profile(PROFILE, entity_moderation=True)
    return t


def concert(title="Jazz Night", days=7, hour=19, **kw):
    return {"title": title, "date": soon(days, hour), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live jazz.",
            "categories": ["music"], "tags": [], "source_url": None, **kw}


def utc(local_iso: str) -> str:
    return datetime.fromisoformat(local_iso).replace(tzinfo=TZ).astimezone(
        timezone.utc).isoformat().replace("+00:00", "Z")


def hand_made_event(slug, title, local_iso, **kw):
    """A live event someone else created: another source_url, no Series."""
    return {"id": 900 + len(slug), "slug": slug, "town_slug": "slt", "title": title,
            "start_at": utc(local_iso), "end_at": None, "status": "published",
            "source_url": "https://someone-else.example/whatever", "venue_id": None,
            "venue_slug": None, "category_slugs": [], "tags": [], **kw}


def posts(town, resource):
    return [c[2] for c in town.api.calls if c[:2] == ("POST", resource)]


def report(town) -> str:
    out = town.report("build")
    with open(out["report_path"]) as f:
        return f.read()


class TestEventsAtShip:
    def test_a_hand_made_live_event_is_bound_not_duplicated(self, town):
        town.api.events["jazz-hand"] = {**hand_made_event("jazz-hand", "Jazz Night!",
                                                          soon(7, 19)),
                                        **{k: None for k in ("image_path", "image_alt")}}
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert(source_url="https://arena.example/jazz")])
        town.run()

        assert posts(town, "events") == []
        row = town.conn.execute("SELECT state, townloops_slug FROM events").fetchone()
        assert tuple(row) == ("shipped", "jazz-hand")
        assert "jazz-hand" in report(town)

    def test_another_showing_the_same_day_is_still_submitted(self, town):
        town.api.events["jazz-matinee"] = hand_made_event("jazz-matinee", "Jazz Night",
                                                          soon(7, 14))
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert(source_url="https://arena.example/jazz")])
        town.run()
        assert [p["title"] for p in posts(town, "events")] == ["Jazz Night"]

    def test_two_plausible_live_events_hold_it_and_the_report_lists_them(self, town):
        town.api.events.update({
            "jazz-a": hand_made_event("jazz-a", "Jazz Night", soon(7, 19)),
            "jazz-b": hand_made_event("jazz-b", "Jazz Night.", soon(7, 19))})
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert(source_url="https://arena.example/jazz")])
        town.run()

        assert posts(town, "events") == []
        extra = json.loads(town.conn.execute("SELECT extra_json FROM events").fetchone()[0])
        assert extra["live_match"]["status"] == "ambiguous"
        assert extra["live_match"]["candidates"] == ["jazz-a", "jazz-b"]
        text = report(town)
        section = text.split("## Ambiguous live matches", 1)[1].split("\n## ", 1)[0]
        assert "Jazz Night" in section and "jazz-a, jazz-b" in section


class TestEntitiesAtShip:
    def test_a_live_community_named_like_the_host_is_reused(self, town):
        town.api.tables["communities"] = {"lake-tahoe-rotary": {
            "id": 501, "slug": "lake-tahoe-rotary", "name": "Lake Tahoe Rotary Club",
            "kind": "group", "description": None, "status": "published"}}
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Benefit Concert", description=(
            "Lake Tahoe Rotary Club presents an evening of music."))])
        town.people["Benefit Concert"] = {
            "host": {"name": "Lake Tahoe Rotary Club", "kind": "community",
                     "kind_quote": "Lake Tahoe Rotary Club presents"}, "people": []}
        town.run()

        assert posts(town, "communities") == []
        assert town.submitted_titles() == ["Benefit Concert"]
        slug = town.conn.execute("SELECT townloops_slug FROM communities").fetchone()[0]
        assert slug == "lake-tahoe-rotary"
        text = report(town)
        assert "community Lake Tahoe Rotary Club: community already live as " \
               "lake-tahoe-rotary" in text

    def test_two_similar_live_venues_are_ambiguous_and_nothing_is_created(self, town):
        town.api.venues.update({
            "tahoe-brewing-company": {"id": 31, "slug": "tahoe-brewing-company",
                                      "name": "Tahoe Brewing Company"},
            "tahoe-brewing-cos": {"id": 32, "slug": "tahoe-brewing-cos",
                                  "name": "Tahoe Brewing Cos Ltd"}})
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert(venue="Tahoe Brewing Co")])
        town.run()

        assert posts(town, "venues") == []
        [sent] = posts(town, "events")
        assert "venue_id" not in sent
        extra = json.loads(town.conn.execute("SELECT extra_json FROM venues").fetchone()[0])
        assert extra["live_match"]["candidates"] == ["tahoe-brewing-company",
                                                     "tahoe-brewing-cos"]
        assert "tahoe-brewing-company, tahoe-brewing-cos" in report(town)


class TestLiveMirrorEnrichment:
    def _mirror(self, town, live, **found):
        from engine.derive_helpers import mirror_live_entity
        from engine.entity_enrich import fill_blanks
        town.api.venues[live["slug"]] = dict(live)
        uid = mirror_live_entity(town.conn, "venue", live)
        fill_blanks(town.conn, "venue", uid, found, "image-proposed")
        town.conn.commit()
        return uid

    def test_a_mirrors_blank_image_is_filled_and_a_set_one_untouched(self, town):
        img = {"image_path": "https://pier.example/pier.jpg", "image_alt": "The pier"}
        pier = self._mirror(town, {"id": 41, "slug": "pier", "name": "The Pier",
                                   "description": "On the lake."}, **img)
        dock = self._mirror(town, {"id": 42, "slug": "dock", "name": "The Dock"},
                            image_path="https://dock.example/ours.jpg", image_alt="Dock")
        # Since the mirror was made, a curator set the dock's own picture and
        # cleared the pier's description.
        town.api.venues["dock"].update(image_path="/static/dock.jpg", image_alt="Their dock")
        town.api.venues["pier"]["description"] = None

        town.live_sync()

        assert town.api.venues["pier"]["image_path"] == img["image_path"]
        assert town.api.venues["pier"]["image_alt"] == "The pier"
        assert town.api.venues["pier"]["description"] is None   # never re-sent
        assert town.api.venues["dock"]["image_path"] == "/static/dock.jpg"
        acts = town.conn.execute("SELECT target, detail FROM agent_activity "
                                 "WHERE action = 'mirror-filled'").fetchall()
        assert [(a[0], a[1].rsplit("set ", 1)[1]) for a in acts] == [
            (f"venue:{pier}", "image_alt, image_path")]
        status = dict(town.conn.execute("SELECT entity_uid, enrichment_status FROM venues"))
        assert status == {pier: "enriched", dock: "live"}

        town.live_sync()   # nothing left to send
        assert len([c for c in town.api.calls if c[0] == "PATCH"]) == 1


class TestReEnrichment:
    def test_misses_older_than_the_setting_are_researched_again(self, town, monkeypatch):
        from engine import entity_enrich
        from engine.derive_helpers import insert_entity
        now = datetime.now(timezone.utc)
        for uid, days in (("old-miss", 20), ("new-miss", 3)):
            insert_entity(town.conn, "businesses", uid, {
                "name": f"Shop {uid}", "source": "derived", "source_kind": "derived",
                "enrichment_status": "not-found",
                "enriched_at": (now - timedelta(days=days)).isoformat(timespec="seconds")})
        town.conn.commit()
        seen = []

        def enrich(conn, kind, uid, town_name, budget, llm=None):
            seen.append(uid)
            entity_enrich.fill_blanks(conn, kind, uid, {"phone": "530-555-0100"}, "enriched")
            return entity_enrich.EnrichOutcome("enriched", ["phone"])
        monkeypatch.setattr(entity_enrich, "enrich_entity", enrich)

        out = town.live_sync()
        assert seen == ["old-miss"] and out["reenriched"] == {"enriched": 1}
        doc = dict(town.profile, enrichment={"retry_not_found_days": 2})
        town.set_profile(doc)
        assert town.live_sync()["reenriched"] == {"enriched": 1}
        assert seen == ["old-miss", "new-miss"]
