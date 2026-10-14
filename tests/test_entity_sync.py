"""Enrichment reaches live: an entity already on TownLoops whose local
content changed since it was last pushed is resynced from its live row
(live values win), then the fields live is missing are PATCHed as a full
body and GET-verified. pushed_hash moves only once live is verified; a live
row that has gone (404) is flagged for the operator, never re-created."""

from __future__ import annotations

import json

import pytest

from engine import entity_enrich as ee
from engine import entity_sync
from engine.derive_helpers import insert_entity
from engine.townloops_api import TownloopsError
from tests.fake_townloops import FakeAPI


@pytest.fixture
def conn(fresh_db):
    return fresh_db


def shipped_venue(conn, uid="v-uid", slug="riva-grill", **kw):
    """A venue we created live earlier, in sync at the time."""
    insert_entity(conn, "venues", uid, {"source": "derived", "source_kind": "derived",
                                        "name": "Riva Grill", "townloops_slug": slug,
                                        "pushed_at": "2026-10-01T00:00:00+00:00", **kw})
    conn.execute("UPDATE venues SET pushed_hash = content_hash WHERE entity_uid = ?", (uid,))


def live_venue(slug="riva-grill", **kw):
    return {"id": 7, "slug": slug, "name": "Riva Grill", "description": None, "address": None,
            "website": None, "phone": None, "accessibility_notes": None, "image_path": None,
            "image_alt": None, "category_slug": None, **kw}


def row(conn, uid="v-uid", table="venues"):
    return dict(conn.execute(f"SELECT * FROM {table} WHERE entity_uid = ?", (uid,)).fetchone())


class TestEnrichmentReachesLive:
    def test_filled_blank_is_patched_live_and_hash_recorded(self, conn):
        api = FakeAPI(venues=[live_venue()])
        shipped_venue(conn)
        ee.fill_blanks(conn, "venue", "v-uid", {"phone": "530-555-0100"}, "enriched")

        res = entity_sync.sync_entity(api, conn, "venue", "v-uid")

        assert res.status == "synced"
        assert api.venues["riva-grill"]["phone"] == "530-555-0100"
        r = row(conn)
        assert r["pushed_hash"] == r["content_hash"]

    def test_patch_is_a_full_body_built_from_the_live_row(self, conn):
        api = FakeAPI(venues=[live_venue(address="1 Lakeshore Blvd")])
        shipped_venue(conn)
        ee.fill_blanks(conn, "venue", "v-uid", {"phone": "530-555-0100"}, "enriched")
        entity_sync.sync_entity(api, conn, "venue", "v-uid")
        [body] = api.patches()
        assert body == {"name": "Riva Grill", "address": "1 Lakeshore Blvd",
                        "phone": "530-555-0100"}

    def test_works_for_every_kind(self, conn):
        insert_entity(conn, "creators", "c-uid", {
            "source": "derived", "source_kind": "derived", "name": "Tahoe Vlogs",
            "townloops_slug": "tahoe-vlogs", "pushed_at": "2026-10-01T00:00:00+00:00"})
        conn.execute("UPDATE creators SET pushed_hash = content_hash")
        api = FakeAPI(entities={"creators": [{"id": 3, "slug": "tahoe-vlogs",
                                              "name": "Tahoe Vlogs", "bio": None}]})
        ee.fill_blanks(conn, "creator", "c-uid", {"bio": "Lake life, weekly."}, "enriched")
        assert entity_sync.sync_entity(api, conn, "creator", "c-uid").status == "synced"
        assert api.tables["creators"]["tahoe-vlogs"]["bio"] == "Lake life, weekly."


class TestFillBlanksBothWays:
    def test_live_value_beats_a_local_one(self, conn):
        api = FakeAPI(venues=[live_venue(description="Lakefront grill since 1979, edited by ops.")])
        shipped_venue(conn)
        ee.fill_blanks(conn, "venue", "v-uid", {"description": "A grill."}, "enriched")
        res = entity_sync.sync_entity(api, conn, "venue", "v-uid")
        assert res.status == "synced" and api.patches() == []
        assert api.venues["riva-grill"]["description"] == "Lakefront grill since 1979, edited by ops."
        assert row(conn)["description"] == "Lakefront grill since 1979, edited by ops."

    def test_local_blank_is_filled_from_live(self, conn):
        api = FakeAPI(venues=[live_venue(address="1 Lakeshore Blvd")])
        shipped_venue(conn)
        ee.fill_blanks(conn, "venue", "v-uid", {"phone": "530-555-0100"}, "enriched")
        res = entity_sync.sync_entity(api, conn, "venue", "v-uid")
        assert res.pulled == ["address"] and res.pushed == ["phone"]
        r = row(conn)
        assert r["address"] == "1 Lakeshore Blvd" and r["pushed_hash"] == r["content_hash"]

    def test_adopted_row_with_nothing_new_is_marked_in_sync_without_a_patch(self, conn):
        api = FakeAPI(venues=[live_venue(phone="530-555-0100")])
        shipped_venue(conn)
        conn.execute("UPDATE venues SET pushed_hash = NULL")   # adopted, never pushed
        assert entity_sync.sync_entity(api, conn, "venue", "v-uid").status == "synced"
        assert api.patches() == []
        r = row(conn)
        assert r["phone"] == "530-555-0100" and r["pushed_hash"] == r["content_hash"]

    def test_unknown_category_is_not_sent(self, conn, monkeypatch):
        from engine import categories
        monkeypatch.setattr(categories, "town_category_slugs", lambda *a: {"food-drink"})
        api = FakeAPI(venues=[live_venue()], categories={"food-drink"})
        shipped_venue(conn, category_slug="music")
        ee.fill_blanks(conn, "venue", "v-uid", {"phone": "530-555-0100"}, "enriched")
        entity_sync.sync_entity(api, conn, "venue", "v-uid")
        assert "category_slug" not in api.patches()[0]

    def test_known_category_is_sent(self, conn, monkeypatch):
        from engine import categories
        monkeypatch.setattr(categories, "town_category_slugs", lambda *a: {"food-drink"})
        api = FakeAPI(venues=[live_venue()], categories={"food-drink"})
        shipped_venue(conn, category_slug="food-drink")
        conn.execute("UPDATE venues SET pushed_hash = NULL")
        entity_sync.sync_entity(api, conn, "venue", "v-uid")
        assert api.venues["riva-grill"]["category_slug"] == "food-drink"

    def test_value_too_long_for_the_api_is_not_sent(self, conn):
        api = FakeAPI(venues=[live_venue()])
        shipped_venue(conn)
        ee.fill_blanks(conn, "venue", "v-uid", {"phone": "5" * 61, "website": "https://riva.example"},
                       "enriched")
        entity_sync.sync_entity(api, conn, "venue", "v-uid")
        assert api.patches() == [{"name": "Riva Grill", "website": "https://riva.example"}]


class TestVerifyAndFlags:
    def _enriched(self, conn):
        shipped_venue(conn)
        ee.fill_blanks(conn, "venue", "v-uid", {"phone": "530-555-0100"}, "enriched")
        return row(conn)["pushed_hash"]

    def _failures(self, conn):
        return conn.execute("SELECT action, target, error FROM agent_activity "
                            "WHERE worker = 'entity-sync' AND ok = 0").fetchall()

    def test_patch_that_doesnt_land_keeps_pushed_hash_and_flags(self, conn):
        api = FakeAPI(venues=[live_venue()], ignore={"phone"})
        before = self._enriched(conn)
        res = entity_sync.sync_entity(api, conn, "venue", "v-uid")
        assert res.status == "mismatch"
        r = row(conn)
        assert r["pushed_hash"] == before != r["content_hash"]
        assert json.loads(r["extra_json"])["live_sync"]["status"] == "mismatch"
        [(action, target, error)] = self._failures(conn)
        assert action == "live-mismatch" and target == "venue:v-uid" and "phone" in error

    def test_flagged_entity_is_not_patched_again(self, conn):
        api = FakeAPI(venues=[live_venue()], ignore={"phone"})
        self._enriched(conn)
        entity_sync.sync_entity(api, conn, "venue", "v-uid")
        assert entity_sync.sync_entity(api, conn, "venue", "v-uid").status == "skipped"
        assert len(api.patches()) == 1

    def test_gone_from_live_is_flagged_not_recreated(self, conn):
        api = FakeAPI()
        before = self._enriched(conn)
        res = entity_sync.sync_entity(api, conn, "venue", "v-uid")
        assert res.status == "missing" and api.creates() == [] and api.patches() == []
        assert row(conn)["pushed_hash"] == before
        assert self._failures(conn)[0]["action"] == "live-missing"

    def test_404_on_patch_is_flagged_not_recreated(self, conn):
        api = FakeAPI(venues=[live_venue()])
        self._enriched(conn)
        real_update = api.update

        def deleted_meanwhile(resource, slug, payload):
            del api.venues[slug]
            return real_update(resource, slug, payload)
        api.update = deleted_meanwhile
        res = entity_sync.sync_entity(api, conn, "venue", "v-uid")
        assert res.status == "missing" and api.creates() == []
        assert json.loads(row(conn)["extra_json"])["live_sync"]["status"] == "missing"

    def test_transient_error_is_retried_later_not_flagged(self, conn):
        api = FakeAPI(venues=[live_venue()])
        before = self._enriched(conn)

        def unavailable(resource, slug, payload):
            raise TownloopsError(503, "unavailable", "try later")
        api.update = unavailable
        assert entity_sync.sync_entity(api, conn, "venue", "v-uid").status == "error"
        r = row(conn)
        assert r["pushed_hash"] == before and "live_sync" not in json.loads(r["extra_json"] or "{}")
        assert entity_sync.needs_sync(r)


class TestSkips:
    def test_unchanged_entity_makes_no_calls(self, conn):
        api = FakeAPI(venues=[live_venue()])
        shipped_venue(conn)
        assert entity_sync.sync_entity(api, conn, "venue", "v-uid").status == "skipped"
        assert api.calls == []

    def test_live_mirror_is_never_pushed(self, conn):
        """A mirror's local image is an operator proposal (image-proposed)."""
        from engine.derive_helpers import mirror_live_entity
        api = FakeAPI(venues=[live_venue()])
        uid = mirror_live_entity(conn, "venue", live_venue())
        ee.fill_blanks(conn, "venue", uid, {"image_path": "https://riva.example/hero.jpg"},
                       "image-proposed")
        assert entity_sync.sync_entity(api, conn, "venue", uid).status == "skipped"
        assert api.calls == []

    def test_never_shipped_entity_is_left_to_event_ship(self, conn):
        api = FakeAPI()
        insert_entity(conn, "venues", "v-uid", {"source": "derived", "source_kind": "derived",
                                                "name": "Riva Grill"})
        assert entity_sync.sync_entity(api, conn, "venue", "v-uid").status == "skipped"


class TestSweep:
    def test_syncs_changed_entities_of_every_kind_up_to_the_limit(self, conn):
        shipped_venue(conn, "v1", "riva-grill")
        shipped_venue(conn, "v2", "bluezone")
        shipped_venue(conn, "v3", "unchanged")
        insert_entity(conn, "businesses", "b1", {
            "source": "derived", "source_kind": "derived", "name": "Cuppa Tahoe",
            "townloops_slug": "cuppa", "pushed_at": "2026-10-01T00:00:00+00:00"})
        api = FakeAPI(venues=[live_venue("riva-grill"), live_venue("bluezone"),
                              live_venue("unchanged")],
                      entities={"businesses": [{"id": 9, "slug": "cuppa", "name": "Cuppa Tahoe"}]})
        for uid in ("v1", "v2"):
            ee.fill_blanks(conn, "venue", uid, {"phone": "530-555-0100"}, "enriched")

        first = entity_sync.sweep(api, conn, limit=2)
        rest = entity_sync.sweep(api, conn, limit=10)

        assert first == {"synced": 2}
        assert rest == {"synced": 1}
        assert api.venues["riva-grill"]["phone"] == api.venues["bluezone"]["phone"] == "530-555-0100"
        assert "unchanged" not in {c[2] for c in api.calls}

    def test_borrowed_property_image_reaches_live(self, conn):
        shipped_venue(conn, website="https://heavenly.example/lodge")
        insert_entity(conn, "businesses", "b1", {
            "source": "derived", "source_kind": "derived", "name": "Heavenly",
            "website": "https://heavenly.example", "image_path": "https://heavenly.example/a.jpg"})
        api = FakeAPI(venues=[live_venue(website="https://heavenly.example/lodge")])
        assert ee.borrow_property_image(conn, "v-uid", [("business", "b1")])
        assert entity_sync.sweep(api, conn) == {"synced": 1}
        assert api.venues["riva-grill"]["image_path"] == "https://heavenly.example/a.jpg"

    def test_cleared_flag_lets_the_entity_sync_again(self, conn):
        api = FakeAPI()
        shipped_venue(conn)
        ee.fill_blanks(conn, "venue", "v-uid", {"phone": "530-555-0100"}, "enriched")
        assert entity_sync.sweep(api, conn) == {"missing": 1}
        assert entity_sync.sweep(api, conn) == {}
        api.venues["riva-grill"] = live_venue()      # operator restored the live row
        assert entity_sync.clear_flag(conn, "venue", "v-uid")
        assert entity_sync.sweep(api, conn) == {"synced": 1}
        assert api.venues["riva-grill"]["phone"] == "530-555-0100"

    def test_rows_that_keep_erroring_dont_starve_the_rest(self, conn):
        for uid, slug in (("a1", "flaky"), ("b2", "riva-grill")):
            shipped_venue(conn, uid, slug)
            ee.fill_blanks(conn, "venue", uid, {"phone": "530-555-0100"}, "enriched")
        api = FakeAPI(venues=[live_venue("flaky"), live_venue("riva-grill")])
        real_get = api.get

        def flaky(resource, slug):
            if slug == "flaky":
                raise TownloopsError(503, "unavailable", "try later")
            return real_get(resource, slug)
        api.get = flaky
        assert entity_sync.sweep(api, conn, limit=1) == {"error": 1}
        assert entity_sync.sweep(api, conn, limit=1) == {"synced": 1}

    def test_town_is_verified_before_sweeping(self, conn):
        api = FakeAPI()
        api.verify_town = lambda *a, **k: (_ for _ in ()).throw(TownloopsError(401, "x", "wrong town"))
        with pytest.raises(TownloopsError):
            entity_sync.sweep(api, conn)


KINDS = {"venue": "venues", "business": "businesses", "community": "communities",
         "performer": "performers", "creator": "creators"}


class TestMergedIntoSurvivor:
    """A curator merged our live row into a published one: its 404 names the
    survivor (`merged_into_slug`, townloops ADR 0009). The local row is
    re-pointed there and synced — not flagged, never re-created."""

    def _shipped(self, conn, kind, uid="e-uid", slug="hermes-slt-old"):
        table = KINDS[kind]
        insert_entity(conn, table, uid, {"source": "derived", "source_kind": "derived",
                                         "name": "Riva Grill", "townloops_slug": slug,
                                         "pushed_at": "2026-10-01T00:00:00+00:00"})
        conn.execute(f"UPDATE {table} SET pushed_hash = content_hash WHERE entity_uid = ?", (uid,))
        ee.fill_blanks(conn, kind, uid, {"website": "https://riva.example"}, "enriched")

    def _merged_api(self, kind, **survivor):
        resource = KINDS[kind]
        api = FakeAPI(entities={resource: [
            {"id": 3, "slug": "hermes-slt-old", "name": "Riva Grill"},
            {"id": 9, "slug": "riva-grill", "name": "Riva Grill", **survivor}]})
        api.merge(resource, "hermes-slt-old", "riva-grill", fields=())
        return api

    @pytest.mark.parametrize("kind", KINDS)
    def test_survivor_is_synced_and_persisted(self, conn, kind):
        api = self._merged_api(kind)
        self._shipped(conn, kind)

        res = entity_sync.sync_entity(api, conn, kind, "e-uid")

        assert res.status == "synced" and api.creates() == []
        assert api.tables[KINDS[kind]]["riva-grill"]["website"] == "https://riva.example"
        r = row(conn, "e-uid", KINDS[kind])
        assert r["townloops_slug"] == "riva-grill"
        assert r["pushed_hash"] == r["content_hash"]
        extra = json.loads(r["extra_json"] or "{}")
        assert "live_sync" not in extra and extra["live_id"] == 9

    def test_later_changes_go_straight_to_the_survivor(self, conn):
        api = self._merged_api("venue")
        self._shipped(conn, "venue")
        entity_sync.sync_entity(api, conn, "venue", "e-uid")
        api.calls.clear()
        ee.fill_blanks(conn, "venue", "e-uid", {"phone": "530-555-0100"}, "enriched")

        assert entity_sync.sync_entity(api, conn, "venue", "e-uid").status == "synced"
        assert api.venues["riva-grill"]["phone"] == "530-555-0100"
        assert {c[2] for c in api.calls} == {"riva-grill"}

    def test_merged_between_read_and_patch_is_retried_on_the_survivor(self, conn):
        api = FakeAPI(venues=[live_venue("hermes-slt-old"),
                              live_venue("riva-grill", id=9, website="https://riva-grill.example")])
        self._shipped(conn, "venue")
        real_update = api.update

        def merged_meanwhile(resource, slug, payload):
            if slug == "hermes-slt-old":
                api.merge(resource, slug, "riva-grill", fields=())
            return real_update(resource, slug, payload)
        api.update = merged_meanwhile

        res = entity_sync.sync_entity(api, conn, "venue", "e-uid")

        assert res.status == "synced" and api.creates() == []
        # Re-read the survivor: its own website is kept, not overwritten.
        assert api.venues["riva-grill"]["website"] == "https://riva-grill.example"
        r = row(conn, "e-uid")
        assert r["townloops_slug"] == "riva-grill" and r["website"] == "https://riva-grill.example"
        assert r["pushed_hash"] == r["content_hash"]
        assert json.loads(r["extra_json"])["live_id"] == 9

    def test_merge_chain_past_the_hop_limit_is_flagged(self, conn):
        from engine.live_lookup import MAX_MERGE_HOPS
        slugs = [f"s{i}" for i in range(MAX_MERGE_HOPS + 2)]
        api = FakeAPI(venues=[live_venue(s) for s in slugs])
        for a, b in zip(slugs, slugs[1:]):
            api.merge("venues", a, b, fields=())
        self._shipped(conn, "venue", slug="s0")
        assert entity_sync.sync_entity(api, conn, "venue", "e-uid").status == "missing"
        assert row(conn, "e-uid")["townloops_slug"] == "s0"

    def test_merge_loop_is_not_followed_forever(self, conn):
        api = FakeAPI()
        api.merged_into.update({("venues", "a"): "b", ("venues", "b"): "a"})
        self._shipped(conn, "venue", slug="a")
        assert entity_sync.sync_entity(api, conn, "venue", "e-uid").status == "missing"
