"""Bare performers/creators wait for research (issue #8): one with only a
name is never created on TownLoops while performer research hasn't verified
it — the event ships on time without it. Once research verifies it (or the
operator accepts a review candidate) it ships on the next event-ship or
entity-live-sync pass, linked locally to the events it already plays."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from engine import performer_research as pr
from engine.derive_helpers import insert_entity, link_event_to_entity
from tests.fake_townloops import FakeAPI
from tests.test_live_lookup import (  # noqa: F401 — fixtures
    EV, _row, _stage, _use, conn, ship, snapshot,
)

ROOT = Path(__file__).resolve().parent.parent
LIVE_VENUE = {"townloops_slug": "the-loft", "source_kind": "live",
              "extra_json": json.dumps({"live_id": 69})}


def add_person(conn, uid, name, kind="performer", **cols):
    table = {"performer": "performers", "creator": "creators"}[kind]
    entity_uid = f"{kind[0]}-{name}"
    insert_entity(conn, table, entity_uid, {"source": "derived", "source_kind": "derived",
                                            "name": name, "enrichment_status": "pending",
                                            **cols})
    link_event_to_entity(conn, uid, kind, entity_uid, role=kind)
    return entity_uid


def posts(api, resource):
    return [c[2] for c in api.calls if c[:2] == ("POST", resource)]


def activity(conn, worker, action):
    return [dict(r) for r in conn.execute(
        "SELECT target, detail, ok FROM agent_activity WHERE worker = ? AND action = ?",
        (worker, action))]


def verify(conn, kind, entity_uid, url="https://ramblers.example"):
    """Performer research's verified answer for this row."""
    pr.apply_finding(conn, kind, entity_uid, pr.Finding(
        "verified", {"website": url}, why="linked from the venue page"))


@pytest.fixture
def live_sync(conn, monkeypatch):
    path = ROOT / "skills/business/entity-live-sync/sync.py"
    spec = importlib.util.spec_from_file_location("entity_live_sync_hold", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "open_db", lambda: conn)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["sync.py", "run", *argv])
        assert mod.main() == 0
    return run


class TestAwaitsResearch:
    @pytest.mark.parametrize("status", ["pending", "not-found", pr.REVIEW_STATUS])
    def test_a_bare_unverified_person_waits(self, status):
        row = {"name": "Direct From Sweden", "enrichment_status": status}
        assert pr.awaits_research("performer", row)
        assert pr.awaits_research("creator", {**row, "kind": "artist"})

    def test_a_verified_person_does_not_wait(self):
        assert not pr.awaits_research("performer", {"name": "X", "enrichment_status": "enriched",
                                                    "website": "https://x.example"})

    @pytest.mark.parametrize("field", ["website", "instagram", "description", "spotify_url"])
    def test_a_person_with_their_own_metadata_does_not_wait(self, field):
        assert not pr.awaits_research("performer", {"name": "X", "enrichment_status": "pending",
                                                    field: "something"})

    def test_a_creator_bio_counts(self):
        assert not pr.awaits_research("creator", {"name": "X", "enrichment_status": "pending",
                                                  "bio": "Potter."})

    def test_an_image_alone_is_not_identity(self):
        assert pr.awaits_research("performer", {"name": "X", "enrichment_status": "pending",
                                                "image_path": "https://x.example/a.jpg"})

    def test_live_mirrors_and_other_kinds_never_wait(self):
        assert not pr.awaits_research("performer", {"name": "X", "source_kind": "live",
                                                    "enrichment_status": "live"})
        assert not pr.awaits_research("venue", {"name": "X", "enrichment_status": "pending"})


class TestEventShipHolds:
    def test_the_event_ships_without_the_bare_performer(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI(venues=[{"id": 69, "slug": "the-loft",
                                                 "name": "Gunbarrel Tavern"}]))
        uid = _stage(ship, monkeypatch, conn, venue=LIVE_VENUE)
        perf = add_person(conn, uid, "Direct From Sweden")
        assert ship.main() == 0
        assert len(posts(api, "events")) == 1
        assert posts(api, "performers") == []
        assert _row(conn, uid)["pushed_at"]
        row = conn.execute("SELECT townloops_slug, pushed_at FROM performers "
                           "WHERE entity_uid = ?", (perf,)).fetchone()
        assert tuple(row) == (None, None)
        [held] = activity(conn, "event-ship", "ship-performer")
        assert "held" in held["detail"] and "research" in held["detail"] and held["ok"]

    def test_a_not_found_bare_creator_is_held_too(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        uid = _stage(ship, monkeypatch, conn)
        add_person(conn, uid, "Jo Potter", kind="creator", enrichment_status="not-found")
        assert ship.main() == 0
        assert posts(api, "creators") == [] and len(posts(api, "events")) == 1

    def test_a_verified_performer_ships_with_the_event(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        uid = _stage(ship, monkeypatch, conn)
        perf = add_person(conn, uid, "Mountain Ramblers")
        verify(conn, "performer", perf)
        assert ship.main() == 0
        [created] = posts(api, "performers")
        assert created["website"] == "https://ramblers.example"

    def test_a_performer_already_live_is_not_held(self, ship, monkeypatch, conn):
        """Shipped before this rule: kept in sync, never treated as new."""
        slug = "hermes-slt-p-Direct From Sw"
        api = _use(monkeypatch, FakeAPI(entities={"performers": [
            {"slug": slug, "name": "Direct From Sweden", "id": 5}]}))
        uid = _stage(ship, monkeypatch, conn)
        add_person(conn, uid, "Direct From Sweden", townloops_slug=slug,
                   pushed_at="2026-10-01T00:00:00+00:00")
        assert ship.main() == 0
        assert posts(api, "performers") == []
        [shipped] = activity(conn, "event-ship", "ship-performer")
        assert shipped["detail"].endswith(slug)


class TestReleasedOnTheNextPass:
    def _held(self, ship, monkeypatch, conn, api):
        _use(monkeypatch, api)
        uid = _stage(ship, monkeypatch, conn)
        perf = add_person(conn, uid, "Mountain Ramblers")
        assert ship.main() == 0
        assert posts(api, "performers") == []
        return uid, perf

    def test_a_verified_performer_ships_on_entity_live_sync(
            self, ship, live_sync, monkeypatch, conn):
        api = FakeAPI()
        uid, perf = self._held(ship, monkeypatch, conn, api)
        verify(conn, "performer", perf)
        live_sync()
        [created] = posts(api, "performers")
        assert (created["name"], created["website"]) == ("Mountain Ramblers",
                                                         "https://ramblers.example")
        row = conn.execute("SELECT * FROM performers WHERE entity_uid = ?", (perf,)).fetchone()
        assert row["townloops_slug"] == api.created_slug("performers") and row["pushed_at"]
        assert row["pushed_hash"] == row["content_hash"]          # synced + verified
        [shipped] = activity(conn, "entity-sync", "ship-performer")
        assert shipped["target"] == f"performer:{perf}"
        assert _row(conn, uid)["townloops_slug"] in shipped["detail"]
        live_sync()                                                # nothing more to do
        assert len(posts(api, "performers")) == 1

    def test_a_still_bare_performer_stays_held(self, ship, live_sync, monkeypatch, conn):
        api = FakeAPI()
        _, perf = self._held(ship, monkeypatch, conn, api)
        pr.apply_finding(conn, "performer", perf, pr.Finding("unresolved", why="nothing"))
        live_sync()
        assert posts(api, "performers") == []

    def test_only_people_of_live_events_are_released(self, live_sync, monkeypatch, conn):
        """A verified performer whose events aren't live yet waits for
        event-ship, which creates it before its event."""
        from engine.event_extract import event_uid, upsert_event
        api = _use(monkeypatch, FakeAPI())
        upsert_event(conn, "Seed", EV)
        uid = event_uid("Seed", EV["title"], EV["date"])
        perf = add_person(conn, uid, "Mountain Ramblers")
        verify(conn, "performer", perf)
        live_sync()
        assert posts(api, "performers") == []

    def test_the_next_event_ship_creates_it(self, ship, monkeypatch, conn):
        api = FakeAPI()
        uid, perf = self._held(ship, monkeypatch, conn, api)
        verify(conn, "performer", perf)
        ev2 = {**EV, "date": "2026-10-16T20:00:00"}
        uid2 = _stage(ship, monkeypatch, conn, ev=ev2)
        link_event_to_entity(conn, uid2, "performer", perf, role="performer")
        assert ship.main() == 0
        assert len(posts(api, "performers")) == 1
        assert uid != uid2

    def test_an_operator_accepted_review_ships_like_a_verified_one(
            self, ship, live_sync, monkeypatch, conn):
        api = FakeAPI()
        _, perf = self._held(ship, monkeypatch, conn, api)
        pr.apply_finding(conn, "performer", perf, pr.Finding(
            "probable", why="no locality", candidates=[pr.Candidate(
                "https://instagram.com/ramblers", "search", read=True, name_match=True,
                role=True, fields={"instagram": "https://instagram.com/ramblers"})]))
        live_sync()
        assert posts(api, "performers") == []                      # in review: held
        pr.resolve(conn, "performer", perf, accept_url="https://instagram.com/ramblers")
        live_sync()
        [created] = posts(api, "performers")
        assert created["instagram"] == "https://instagram.com/ramblers"

    def test_failed_releases_go_last_so_they_cannot_starve_the_rest(
            self, ship, monkeypatch, conn):
        from engine import entity_ship
        api = FakeAPI()
        uid, a = self._held(ship, monkeypatch, conn, api)
        b = add_person(conn, uid, "Zydeco Zack")
        for p in (a, b):
            verify(conn, "performer", p)
        broken = {"name": "Mountain Ramblers"}
        create = api.create

        def flaky(resource, payload, idempotency_key=None):
            if resource == "performers" and payload["name"] in broken.values():
                raise RuntimeError("503")
            return create(resource, payload, idempotency_key=idempotency_key)
        monkeypatch.setattr(api, "create", flaky)

        assert entity_ship.ship_released(api, conn, limit=1) == {"failed": 1}
        extra = json.loads(conn.execute("SELECT extra_json FROM performers WHERE entity_uid = ?",
                                        (a,)).fetchone()[0])
        assert extra["sync_error_at"]
        assert entity_ship.ship_released(api, conn, limit=1) == {"shipped": 1}
        assert [p["name"] for p in posts(api, "performers")][-1] == "Zydeco Zack"
        broken.clear()
        assert entity_ship.ship_released(api, conn, limit=1) == {"shipped": 1}
        extra = json.loads(conn.execute("SELECT extra_json FROM performers WHERE entity_uid = ?",
                                        (a,)).fetchone()[0] or "{}")
        assert "sync_error_at" not in extra

    def test_a_kind_filtered_sync_releases_only_that_kind(
            self, ship, live_sync, monkeypatch, conn):
        api = FakeAPI()
        uid, perf = self._held(ship, monkeypatch, conn, api)
        verify(conn, "performer", perf)
        live_sync("--kind", "venue")
        assert posts(api, "performers") == []
        live_sync("--kind", "creator")
        assert posts(api, "performers") == []
        live_sync("--kind", "performer")
        assert len(posts(api, "performers")) == 1

    def test_an_operator_rejected_review_stays_held(self, ship, live_sync, monkeypatch, conn):
        api = FakeAPI()
        _, perf = self._held(ship, monkeypatch, conn, api)
        pr.apply_finding(conn, "performer", perf, pr.Finding("ambiguous", why="two"))
        pr.resolve(conn, "performer", perf)
        live_sync()
        assert posts(api, "performers") == []
