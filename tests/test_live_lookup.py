"""event-ship looks for a record on TownLoops before creating one: an event
by slug, then by source_url at the same time; an entity by slug. A record
already live is updated (events) or adopted (entities) instead of POSTed,
and a 409 means "go find the live row", not "fine". Events point at their
venue by id, so the server's name lookup can't hit duplicate venue names."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from engine.derive_helpers import insert_entity, link_event_to_entity
from engine.event_extract import event_uid, upsert_event
from engine.live_lookup import find_live_entity, find_live_event
from engine.townloops_api import TownloopsError
from tests.fake_townloops import FakeAPI
from tests.test_categories import SLT_CATEGORIES


START = "2026-10-10T03:00:00+00:00"   # 2026-10-09 20:00 in America/Los_Angeles
TZ = __import__("dateutil.tz").tz.gettz("America/Los_Angeles")
SRC = "https://tahoe.example/events/jazz-night"


_IDS = iter(range(500, 10_000))


def live_event(slug, start=START, source_url=SRC, **kw):
    return {"id": next(_IDS), "slug": slug, "town_slug": "slt", "title": "Jazz Night",
            "start_at": start, "end_at": None, "source_url": source_url,
            "venue_id": None, "venue_slug": None, "category_slugs": [], "tags": [], **kw}


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------

class TestFindLiveEvent:
    def test_found_by_slug(self):
        api = FakeAPI([live_event("hermes-jazz", source_url=None)])
        m = find_live_event(api, slug="hermes-jazz", source_url=SRC, start_at=START)
        assert m.row["slug"] == "hermes-jazz" and not m.ambiguous

    def test_found_by_source_url_under_another_slug(self):
        api = FakeAPI([live_event("jazz-night-2026-10-09")])
        m = find_live_event(api, slug="hermes-jazz", source_url=SRC, start_at=START)
        assert m.row["slug"] == "jazz-night-2026-10-09"

    def test_source_url_match_ignores_a_trailing_slash(self):
        api = FakeAPI([live_event("jazz", source_url=SRC + "/")])
        assert find_live_event(api, slug="hermes-jazz", source_url=SRC, start_at=START).row

    def test_not_found(self):
        api = FakeAPI([live_event("other", source_url="https://tahoe.example/events/other")])
        m = find_live_event(api, slug="hermes-jazz", source_url=SRC, start_at=START)
        assert m.row is None and not m.ambiguous

    def test_same_series_on_another_day_is_not_this_event(self):
        """Recurring series share a source_url — only the same instance counts."""
        api = FakeAPI([live_event("jazz-wk2", start="2026-10-17T03:00:00+00:00")])
        assert find_live_event(api, slug="hermes-jazz", source_url=SRC, start_at=START).row is None

    def test_same_url_same_day_prefers_the_same_start(self):
        api = FakeAPI([live_event("matinee", start="2026-10-09T21:00:00+00:00"),
                       live_event("evening")])
        assert find_live_event(api, slug="hermes-jazz", source_url=SRC,
                               start_at=START).row["slug"] == "evening"

    def test_multiple_live_rows_are_ambiguous(self):
        api = FakeAPI([live_event("jazz-a"), live_event("jazz-b")])
        m = find_live_event(api, slug="hermes-jazz", source_url=SRC, start_at=START)
        assert m.row is None
        assert sorted(r["slug"] for r in m.ambiguous) == ["jazz-a", "jazz-b"]

    def test_busy_window_is_read_past_the_first_page(self):
        api = FakeAPI([live_event(f"other-{i}", source_url=f"https://x.example/{i}")
                       for i in range(205)] + [live_event("jazz-live")])
        assert find_live_event(api, slug="hermes-jazz", source_url=SRC,
                               start_at=START).row["slug"] == "jazz-live"

    def test_api_errors_other_than_404_raise(self):
        api = FakeAPI()

        def boom(*a, **k):
            raise TownloopsError(401, "unauthorized", "bad token")
        api.get = boom
        with pytest.raises(TownloopsError):
            find_live_event(api, slug="hermes-jazz", source_url=SRC, start_at=START)


class TestFindLiveEntity:
    def test_found_and_not_found(self):
        api = FakeAPI(venues=[{"id": 7, "slug": "riva-grill", "name": "Riva Grill"}])
        assert find_live_entity(api, "venues", "riva-grill")["id"] == 7
        assert find_live_entity(api, "venues", "hermes-x") is None


# --------------------------------------------------------------------------
# event-ship
# --------------------------------------------------------------------------

@pytest.fixture
def snapshot(tmp_state_dir):
    (tmp_state_dir / "categories.json").write_text(json.dumps(SLT_CATEGORIES))


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


@pytest.fixture
def ship(conn, monkeypatch, snapshot):
    path = Path(__file__).resolve().parent.parent / "skills/business/event-ship/ship.py"
    spec = importlib.util.spec_from_file_location("event_ship_lookup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "open_db", lambda: conn)
    for k, v in (("TOWNLOOPS_API_URL", "https://tl.example"), ("TOWNLOOPS_API_KEY", "k"),
                 ("TOWNLOOPS_TOWN_SLUG", "slt")):
        monkeypatch.setenv(k, v)
    return mod


def _use(monkeypatch, api):
    import engine.townloops_api as tl
    monkeypatch.setattr(tl, "Client", lambda: api)
    return api


EV = {"title": "Jazz Night", "date": "2026-10-09T20:00:00", "source_url": SRC,
      "description": "Weekly jazz in the lounge.", "categories": ["music"], "tags": ["free"]}


def _stage(ship, monkeypatch, conn, ev=EV, venue=None):
    """Upsert the event; with `venue` (fields dict), derive + link that venue."""
    upsert_event(conn, "Seed", ev)
    uid = event_uid("Seed", ev["title"], ev["date"])
    if venue:
        insert_entity(conn, "venues", "v-uid", {"source": "derived", "source_kind": "derived",
                                                "name": "Gunbarrel Tavern", **venue})
        link_event_to_entity(conn, uid, "venue", "v-uid")
        conn.execute("UPDATE events SET derived_venue_uid = 'v-uid' WHERE event_uid = ?", (uid,))
    monkeypatch.setattr(ship, "parse_event_uid_arg", lambda: uid)
    return uid


def _row(conn, uid):
    return conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone()


class TestShipEventLookup:
    def test_not_found_is_created(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        uid = _stage(ship, monkeypatch, conn)
        assert ship.main() == 0
        [payload] = api.creates()
        assert payload["source_url"] == SRC
        assert _row(conn, uid)["townloops_slug"] == payload["slug"]

    def test_live_by_source_url_is_patched_not_created(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live", category_slugs=["arts"])]))
        uid = _stage(ship, monkeypatch, conn)
        assert ship.main() == 0
        assert api.creates() == []
        [patch] = api.patches()
        assert patch["description"] == "Weekly jazz in the lounge."   # blank live field filled
        assert patch["title"] == "Jazz Night"                         # full body
        assert sorted(patch["category_slugs"]) == ["arts", "live-music"]  # live set kept
        assert _row(conn, uid)["townloops_slug"] == "jazz-live"
        assert api.events["jazz-live"]["tags"] == ["free"]

    def test_end_is_only_filled_on_the_same_start(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event(
            "jazz-live", start="2026-10-10T01:00:00+00:00", description="x",
            category_slugs=["live-music"], tags=["free"])]))
        _stage(ship, monkeypatch, conn, ev={**EV, "end_at": "2026-10-09T23:00:00"})
        assert ship.main() == 0
        assert api.patches() == []

    def test_live_values_are_not_overwritten(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event(
            "jazz-live", title="Jazz Night w/ The Trio", description="Operator copy.",
            category_slugs=["live-music"], tags=["free"])]))
        _stage(ship, monkeypatch, conn)
        assert ship.main() == 0
        assert api.creates() == [] and api.patches() == []    # nothing to add
        assert api.events["jazz-live"]["title"] == "Jazz Night w/ The Trio"

    def test_live_by_own_slug_is_patched_not_created(self, ship, monkeypatch, conn):
        api = FakeAPI()
        _use(monkeypatch, api)
        uid = _stage(ship, monkeypatch, conn)
        slug = ship._slug_for("event", uid)
        api.events[slug] = live_event(slug, source_url=None)
        assert ship.main() == 0
        assert api.creates() == []
        assert api.patches()[0]["source_url"] == SRC

    def test_multiple_live_rows_park_the_event(self, ship, monkeypatch, conn):
        from engine import workqueue
        api = _use(monkeypatch, FakeAPI([live_event("jazz-a"), live_event("jazz-b")]))
        uid = _stage(ship, monkeypatch, conn)
        conn.execute("UPDATE events SET state = 'entities-enriched' WHERE event_uid = ?", (uid,))
        workqueue.enqueue(conn, uid, "ship")
        assert ship.main() != 0
        assert api.creates() == [] and api.patches() == []
        assert _row(conn, uid)["state"] == "stuck-entities-enriched"
        act = conn.execute("SELECT error FROM agent_activity WHERE worker = 'event-ship' "
                           "AND action = 'find-live-event' AND ok = 0").fetchone()
        assert "jazz-a" in act["error"] and "jazz-b" in act["error"]

    def test_409_with_no_live_row_found_is_not_success(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        uid = _stage(ship, monkeypatch, conn)

        def conflict(*a, **k):
            raise TownloopsError(409, "slug_taken", "slug taken")
        api.create = conflict
        assert ship.main() != 0
        assert _row(conn, uid)["pushed_at"] is None

    def test_409_reconciles_to_the_live_row(self, ship, monkeypatch, conn):
        """A row that appears between lookup and POST (another worker)."""
        api = _use(monkeypatch, FakeAPI())
        uid = _stage(ship, monkeypatch, conn)
        real_create = api.create

        def racing_create(resource, payload, idempotency_key=None):
            api.events[payload["slug"]] = live_event(payload["slug"])
            return real_create(resource, payload, idempotency_key)
        api.create = racing_create
        assert ship.main() == 0
        assert _row(conn, uid)["townloops_slug"] == ship._slug_for("event", uid)
        assert api.patches()[0]["description"] == "Weekly jazz in the lounge."

    def test_lookup_error_is_a_retry_not_a_create(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        _stage(ship, monkeypatch, conn)

        def down(*a, **k):
            raise TownloopsError(0, "network", "exhausted retries")
        api.list = down
        assert ship.main() != 0
        assert api.creates() == []


class TestEventVenueById:
    VENUES = [{"id": 69, "slug": "gunbarrel-tavern", "name": "Gunbarrel Tavern"},
              {"id": 70, "slug": "gunbarrel-tavern-2", "name": "Gunbarrel Tavern"}]

    def test_shipped_venue_is_referenced_by_id(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI(venues=self.VENUES))
        _stage(ship, monkeypatch, conn, venue={"townloops_slug": "gunbarrel-tavern",
                                               "source_kind": "live",
                                               "extra_json": json.dumps({"live_id": 69})})
        assert ship.main() == 0
        [payload] = api.creates()
        assert payload["venue_id"] == 69
        assert "venue_slug" not in payload
        [created] = api.events.values()
        assert created["venue_slug"] == "gunbarrel-tavern"

    def test_new_venue_id_comes_from_its_create(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        _stage(ship, monkeypatch, conn, venue={"address": "1 Main St"})
        assert ship.main() == 0
        venue_post, event_post = api.creates()
        assert event_post["venue_id"] == api.venues[venue_post["slug"]]["id"]

    def test_venue_filed_under_another_town_binds_by_name(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        real_create = api.create

        def ghost_town(resource, payload, idempotency_key=None):
            row = real_create(resource, payload, idempotency_key)
            if resource == "venues":
                api.venues[row["slug"]]["town"] = row["town_slug"] = "reno"
            return row
        api.create = ghost_town
        _stage(ship, monkeypatch, conn, ev={**EV, "venue": "Gunbarrel Tavern"},
               venue={"address": "1 Main St"})
        assert ship.main() == 0
        event_post = api.creates()[-1]
        assert "venue_id" not in event_post
        assert event_post["venue_name"] == "Gunbarrel Tavern"
        row = conn.execute("SELECT extra_json FROM venues WHERE entity_uid = 'v-uid'").fetchone()
        assert "live_id" not in json.loads(row["extra_json"] or "{}")

    def test_unresolved_venue_falls_back_to_name(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        _stage(ship, monkeypatch, conn, ev={**EV, "venue": "Somewhere Hall"})
        assert ship.main() == 0
        assert api.creates()[0]["venue_name"] == "Somewhere Hall"

    def test_ambiguous_venue_name_fails_with_a_clear_reason(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI(venues=self.VENUES))
        uid = _stage(ship, monkeypatch, conn, ev={**EV, "venue": "Gunbarrel Tavern"})
        assert ship.main() != 0
        act = conn.execute("SELECT error FROM agent_activity WHERE target = ? AND ok = 0 "
                           "ORDER BY rowid DESC LIMIT 1", (uid,)).fetchone()
        assert "several live venues" in act["error"]


class TestShipEntityLookup:
    def _venue(self, conn, **kw):
        insert_entity(conn, "venues", "v-uid", {"source": "derived", "source_kind": "derived",
                                                "name": "Riva Grill", **kw})

    def _venue_row(self, conn):
        return conn.execute("SELECT * FROM venues WHERE entity_uid = 'v-uid'").fetchone()

    def test_live_entity_is_adopted_not_posted(self, ship, conn):
        api = FakeAPI(venues=[{"id": 7, "slug": "riva-grill", "name": "Riva Grill",
                               "address": "1 Lakeshore Blvd"}])
        self._venue(conn, townloops_slug="riva-grill", phone="530-555-0100")
        assert ship._ship_entity(api, conn, "venue", "v-uid") == "riva-grill"
        assert api.creates() == []
        row = self._venue_row(conn)
        assert json.loads(row["extra_json"])["live_id"] == 7
        # synced both ways: live's address kept locally, our phone filled live
        assert row["address"] == "1 Lakeshore Blvd"
        assert api.venues["riva-grill"]["phone"] == "530-555-0100"
        assert row["pushed_hash"] == row["content_hash"]

    def test_created_entity_gets_what_create_doesnt_send(self, ship, conn):
        """The create payload has no image_path; the post-create sync fills it."""
        api = FakeAPI()
        self._venue(conn, image_path="https://riva.example/hero.jpg")
        slug = ship._ship_entity(api, conn, "venue", "v-uid")
        assert api.venues[slug]["image_path"] == "https://riva.example/hero.jpg"
        row = self._venue_row(conn)
        assert row["pushed_hash"] == row["content_hash"]

    def test_enriched_after_shipping_is_patched_on_the_next_ship(self, ship, conn):
        from engine import entity_enrich
        api = FakeAPI()
        self._venue(conn)
        slug = ship._ship_entity(api, conn, "venue", "v-uid")
        entity_enrich.fill_blanks(conn, "venue", "v-uid", {"phone": "530-555-0100"}, "enriched")
        assert ship._ship_entity(api, conn, "venue", "v-uid") == slug
        assert api.venues[slug]["phone"] == "530-555-0100"
        assert len(api.creates()) == 1

    def test_cross_town_entity_is_neither_synced_nor_flagged(self, ship, conn):
        """A venue the server filed under another town 404s on our town's
        GETs — that's not 'gone', and its pushed_hash can't be verified."""
        from engine import entity_enrich, entity_sync
        api = FakeAPI()
        real_create = api.create

        def ghost_town(resource, payload, idempotency_key=None):
            row = real_create(resource, payload, idempotency_key)
            api.venues[row["slug"]]["town"] = "reno"
            return {**row, "town_slug": "reno"}
        api.create = ghost_town
        self._venue(conn)
        slug = ship._ship_entity(api, conn, "venue", "v-uid")
        row = self._venue_row(conn)
        assert row["pushed_hash"] is None
        assert json.loads(row["extra_json"])["cross_town"] == "reno"
        entity_enrich.fill_blanks(conn, "venue", "v-uid", {"phone": "530-555-0100"}, "enriched")
        assert ship._ship_entity(api, conn, "venue", "v-uid") == slug
        assert entity_sync.sweep(api, conn) == {}
        assert "live_sync" not in json.loads(self._venue_row(conn)["extra_json"])
        assert len(api.creates()) == 1

    def test_flagged_entity_is_not_flagged_again_on_every_ship(self, ship, conn):
        api = FakeAPI()
        self._venue(conn, townloops_slug="riva-grill", pushed_at="2026-10-01T00:00:00+00:00")
        ship._ship_entity(api, conn, "venue", "v-uid")
        ship._ship_entity(api, conn, "venue", "v-uid")
        n = conn.execute("SELECT COUNT(*) FROM agent_activity WHERE worker = 'entity-sync'").fetchone()[0]
        assert n == 1

    def test_shipped_entity_gone_from_live_is_flagged_not_recreated(self, ship, conn):
        api = FakeAPI()
        self._venue(conn, townloops_slug="riva-grill", pushed_at="2026-10-01T00:00:00+00:00")
        assert ship._ship_entity(api, conn, "venue", "v-uid") is None
        assert api.creates() == []
        assert json.loads(self._venue_row(conn)["extra_json"])["live_sync"]["status"] == "missing"

    def test_not_found_is_created_and_its_live_id_recorded(self, ship, conn):
        api = FakeAPI()
        self._venue(conn)
        slug = ship._ship_entity(api, conn, "venue", "v-uid")
        [payload] = api.creates()
        assert slug == payload["slug"]
        row = self._venue_row(conn)
        assert json.loads(row["extra_json"])["live_id"] == api.venues[slug]["id"]
        assert row["pushed_hash"] == row["content_hash"]

    def test_409_without_a_live_row_is_a_failure(self, ship, conn):
        api = FakeAPI()
        self._venue(conn)

        def conflict(*a, **k):
            raise TownloopsError(409, "slug_taken", "slug taken")
        api.create = conflict
        assert ship._ship_entity(api, conn, "venue", "v-uid") is None
        assert self._venue_row(conn)["townloops_slug"] is None

    def test_409_reconciles_to_the_live_row(self, ship, conn):
        api = FakeAPI()
        self._venue(conn)
        real_create = api.create

        def racing_create(resource, payload, idempotency_key=None):
            api.venues[payload["slug"]] = {"id": 42, "slug": payload["slug"], "name": "Riva Grill"}
            return real_create(resource, payload, idempotency_key)
        api.create = racing_create
        assert ship._ship_entity(api, conn, "venue", "v-uid")
        assert json.loads(self._venue_row(conn)["extra_json"])["live_id"] == 42

    def test_lookup_error_is_a_failure_not_a_create(self, ship, conn):
        api = FakeAPI()
        self._venue(conn)

        def down(*a, **k):
            raise TownloopsError(401, "unauthorized", "bad token")
        api.get = down
        assert ship._ship_entity(api, conn, "venue", "v-uid") is None
        assert api.creates() == []

    def test_existing_extra_json_is_kept(self, ship, conn):
        api = FakeAPI(venues=[{"id": 7, "slug": "riva-grill", "name": "Riva Grill"}])
        self._venue(conn, townloops_slug="riva-grill", extra_json=json.dumps({"note": "x"}))
        ship._ship_entity(api, conn, "venue", "v-uid")
        assert json.loads(self._venue_row(conn)["extra_json"]) == {"note": "x", "live_id": 7}


class TestFindBySeriesDay:
    """A Series' occurrence on the same town-local date is the event, whatever
    its source_url or start time — a second source's copy, not a new date."""

    SERIES = [{"id": 1, "slug": "jazz-night", "title": "Jazz Night"}]

    def test_same_local_day_in_the_series_is_found(self):
        api = FakeAPI([live_event("jazz-oct-9", start="2026-10-10T02:00:00+00:00",
                                  source_url="https://other.example/x",
                                  series_slug="jazz-night", series_id=1)], series=self.SERIES)
        m = find_live_event(api, slug="hermes-jazz", source_url=SRC, start_at=START,
                            series_slug="jazz-night", tz=TZ)
        assert m.row["slug"] == "jazz-oct-9" and m.matched_by == "series-day"

    def test_another_day_in_the_series_is_not(self):
        api = FakeAPI([live_event("jazz-oct-10", start="2026-10-11T03:00:00+00:00",
                                  source_url="https://other.example/x",
                                  series_slug="jazz-night", series_id=1)], series=self.SERIES)
        m = find_live_event(api, slug="hermes-jazz", source_url=SRC, start_at=START,
                            series_slug="jazz-night", tz=TZ)
        assert m.row is None and not m.ambiguous

    def test_two_on_the_day_are_ambiguous(self):
        api = FakeAPI([live_event(s, source_url=f"https://o.example/{s}",
                                  series_slug="jazz-night", series_id=1)
                       for s in ("jazz-a", "jazz-b")], series=self.SERIES)
        m = find_live_event(api, slug="hermes-jazz", source_url=SRC, start_at=START,
                            series_slug="jazz-night", tz=TZ)
        assert m.row is None and sorted(r["slug"] for r in m.ambiguous) == ["jazz-a", "jazz-b"]

    def test_slug_still_wins(self):
        api = FakeAPI([live_event("hermes-jazz", source_url=None)], series=self.SERIES)
        m = find_live_event(api, slug="hermes-jazz", source_url=SRC, start_at=START,
                            series_slug="jazz-night", tz=TZ)
        assert m.row["slug"] == "hermes-jazz"
        assert not [c for c in api.calls if c[0] == "LIST"]


# --------------------------------------------------------------------------
# merged records (townloops ADR 0009): a 404 naming `merged_into_slug`
# --------------------------------------------------------------------------

class TestMergeSurvivor:
    def test_survivor_is_read_from_a_404s_details(self):
        from engine.live_lookup import merged_into, merged_into_slug
        assert merged_into_slug(TownloopsError(404, "not_found", "x",
                                               {"merged_into_slug": "riva"})) == "riva"
        assert merged_into_slug(TownloopsError(404, "not_found", "x")) is None
        assert merged_into_slug(TownloopsError(409, "slug_taken", "x",
                                               {"merged_into_slug": "riva"})) is None
        assert merged_into({"merged_into_slug": "riva"}) == "riva"
        assert merged_into(None) is None and merged_into({"merged_into_slug": ""}) is None

    def test_follow_merges_reports_an_unresolved_chain(self):
        from engine.live_lookup import follow_merges
        names = {"a": "b", "b": "a"}
        tried, repointed = [], []

        def attempt(slug):
            tried.append(slug)
            return slug.upper(), names.get(slug)
        assert follow_merges(attempt, "a", repointed.append) == ("B", "b", "a")
        assert tried == ["a", "b"] and repointed == ["b"]

    def test_lookup_follows_the_survivor(self):
        api = FakeAPI(venues=[{"id": 7, "slug": "old", "name": "Riva"},
                              {"id": 9, "slug": "riva", "name": "Riva"}])
        api.merge("venues", "old", "riva", fields=())
        assert find_live_entity(api, "venues", "old")["slug"] == "riva"

    def test_a_chain_is_followed_up_to_the_hop_limit(self):
        from engine.live_lookup import MAX_MERGE_HOPS
        slugs = [f"s{i}" for i in range(MAX_MERGE_HOPS + 2)]
        api = FakeAPI(venues=[{"id": i, "slug": s, "name": "Riva"} for i, s in enumerate(slugs)])
        for a, b in zip(slugs, slugs[1:]):
            api.merge("venues", a, b, fields=())
        assert find_live_entity(api, "venues", slugs[1])["slug"] == slugs[-1]
        assert find_live_entity(api, "venues", slugs[0]) is None

    def test_a_loop_ends(self):
        api = FakeAPI()
        api.merged_into.update({("venues", "a"): "b", ("venues", "b"): "a"})
        assert find_live_entity(api, "venues", "a") is None


ENTITY_KINDS = {"venue": "venues", "business": "businesses", "community": "communities",
                "performer": "performers", "creator": "creators"}


class TestShipMergedRecords:
    """An entity we shipped that a curator merged into a published one: ship
    adopts the survivor (slug persisted, kept up to date) — never re-POSTs,
    never flags it as gone. (Event 404s never name a survivor.)"""

    @pytest.mark.parametrize("kind", ENTITY_KINDS)
    def test_merged_entity_is_repointed_and_kept_updated(self, ship, conn, kind):
        from engine import entity_enrich
        table = ENTITY_KINDS[kind]
        api = FakeAPI(entities={table: [{"id": 3, "slug": "hermes-slt-old", "name": "Riva Grill"},
                                        {"id": 9, "slug": "riva-grill", "name": "Riva Grill"}]})
        api.merge(table, "hermes-slt-old", "riva-grill", fields=())
        insert_entity(conn, table, "e-uid", {"source": "derived", "source_kind": "derived",
                                             "name": "Riva Grill", "townloops_slug": "hermes-slt-old",
                                             "pushed_at": "2026-10-01T00:00:00+00:00"})

        assert ship._ship_entity(api, conn, kind, "e-uid") == "riva-grill"
        entity_enrich.fill_blanks(conn, kind, "e-uid", {"website": "https://riva.example"},
                                  "enriched")
        api.calls.clear()
        assert ship._ship_entity(api, conn, kind, "e-uid") == "riva-grill"

        assert api.creates() == []
        assert api.tables[table]["riva-grill"]["website"] == "https://riva.example"
        assert {c[2] for c in api.calls} == {"riva-grill"}     # no detour via the old slug
        row = conn.execute(f"SELECT * FROM {table} WHERE entity_uid = 'e-uid'").fetchone()
        assert row["townloops_slug"] == "riva-grill"
        assert row["pushed_hash"] == row["content_hash"]
        extra = json.loads(row["extra_json"])
        assert extra["live_id"] == 9 and "live_sync" not in extra
