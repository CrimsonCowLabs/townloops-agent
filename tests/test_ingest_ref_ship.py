"""The Ingest Ref end to end (#63, townloops ADR 0011): every create carries
the agent's stable per-row ref and never a slug, so a ship cycle re-binds to
what is already live — after the local cache is wiped, after TownLoops renames
rows, and for rows shipped before refs (which are given theirs, once)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from engine import db as db_mod
from engine import entity_sync, ingest_ref
from engine.derive_helpers import insert_entity, link_event_to_entity
from engine.event_extract import event_uid, upsert_event
from tests.fake_townloops import FakeAPI
from tests.test_live_lookup import _use, conn, ship, snapshot  # noqa: F401 — fixtures


EVENTS = [
    {"title": "Jazz Night", "date": "2026-10-09T20:00:00", "description": "Weekly jazz.",
     "source_url": "https://tahoe.example/events/jazz-night", "categories": ["music"]},
    {"title": "Trivia Thursday", "date": "2026-10-15T19:00:00", "description": "Pub quiz.",
     "source_url": "https://tahoe.example/events/trivia", "categories": ["music"]},
]
# (kind, table, uid, role, fields) — the same deterministic uids every derive.
ENTITIES = [
    ("venue", "venues", "v-0123456789abcdef", "primary",
     {"name": "Gunbarrel Tavern", "address": "1 Main St"}),
    ("business", "businesses", "b-0123456789abcdef", "host",
     {"name": "Riva Grill", "website": "https://riva.example"}),
    ("community", "communities", "c-0123456789abcdef", "host",
     {"name": "Rotary Club of Tahoe", "website": "https://rotary.example"}),
    ("performer", "performers", "p-0123456789abcdef", "performer",
     {"name": "Mountain Ramblers", "website": "https://ramblers.example"}),
    ("creator", "creators", "k-0123456789abcdef", "creator",
     {"name": "Tahoe Vlogs", "website": "https://vlogs.example"}),
]
RESOURCES = ("events", "venues", "businesses", "communities", "performers", "creators")


def derive(conn) -> list[str]:
    """Stage both events and their entities, as derivation would — no
    TownLoops slugs, nothing pushed."""
    uids = []
    for ev in EVENTS:
        upsert_event(conn, "Seed", ev)
        uid = event_uid("Seed", ev["title"], ev["date"])
        for kind, table, euid, role, fields in ENTITIES:
            insert_entity(conn, table, euid, {"source": "derived", "source_kind": "derived",
                                              **fields})
            link_event_to_entity(conn, uid, kind, euid, role)
        conn.execute("UPDATE events SET derived_venue_uid = ?, state = 'entities-enriched' "
                     "WHERE event_uid = ?", (ENTITIES[0][2], uid))
        uids.append(uid)
    return uids


def ship_all(ship, monkeypatch, uids):  # noqa: F811
    for uid in uids:
        monkeypatch.setattr(ship, "parse_event_uid_arg", lambda uid=uid: uid)
        assert ship.main() == 0


def local_slugs(conn) -> dict[str, str]:
    out = {r["event_uid"]: r["townloops_slug"]
           for r in conn.execute("SELECT event_uid, townloops_slug FROM events")}
    for _, table, euid, _, _ in ENTITIES:
        out[euid] = conn.execute(f"SELECT townloops_slug FROM {table} WHERE entity_uid = ?",
                                 (euid,)).fetchone()[0]
    return out


def local_refs(conn) -> dict[str, str]:
    out = {r["event_uid"]: r["ingest_ref"]
           for r in conn.execute("SELECT event_uid, ingest_ref FROM events")}
    for _, table, euid, _, _ in ENTITIES:
        out[euid] = conn.execute(f"SELECT ingest_ref FROM {table} WHERE entity_uid = ?",
                                 (euid,)).fetchone()[0]
    return out


def failures(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT worker, action, error FROM agent_activity WHERE ok = 0")]


def flags(conn) -> list[str]:
    return [f"{table}:{euid}" for _, table, euid, _, _ in ENTITIES
            if "live_sync" in json.loads(conn.execute(
                f"SELECT COALESCE(extra_json, '{{}}') FROM {table} WHERE entity_uid = ?",
                (euid,)).fetchone()[0])]


@pytest.fixture
def wiped(tmp_state_dir):
    """A second, freshly migrated local DB — the cache after a wipe."""
    c = db_mod.connect(str(tmp_state_dir / "wiped.db"))
    db_mod.run_migrations(c)
    c.row_factory = sqlite3.Row
    c.isolation_level = None
    yield c
    c.close()


def test_every_create_carries_its_ref_and_no_slug(ship, monkeypatch, conn):  # noqa: F811
    api = _use(monkeypatch, FakeAPI())
    uids = derive(conn)
    ship_all(ship, monkeypatch, uids)
    posts = [c for c in api.calls if c[0] == "POST"]
    assert {c[1] for c in posts} == set(RESOURCES)               # all six kinds created
    assert all("slug" not in c[2] for c in posts)
    assert sorted(c[2]["ingest_ref"] for c in posts) == sorted(
        ingest_ref.ref_for(u) for u in uids + [e[2] for e in ENTITIES])
    # Each local row is bound to the slug TownLoops derived and knows its ref.
    for uid, slug in local_slugs(conn).items():
        assert slug and api.ref_holder(_resource(uid, uids), ingest_ref.ref_for(uid)) == slug
    assert local_refs(conn) == {u: ingest_ref.ref_for(u) for u in local_refs(conn)}
    assert failures(conn) == []


def _resource(uid, event_uids):
    if uid in event_uids:
        return "events"
    return next(table for _, table, euid, _, _ in ENTITIES if euid == uid)


class TestWipedCache:
    """The test the whole of #63 exists to pass: a ship cycle from an emptied
    local cache creates nothing new on TownLoops and re-binds every row."""

    def test_reship_from_a_wiped_cache_creates_nothing(
            self, ship, monkeypatch, conn, wiped):  # noqa: F811
        api = _use(monkeypatch, FakeAPI())
        uids = derive(conn)
        ship_all(ship, monkeypatch, uids)
        before = dict(local_slugs(conn))
        created = list(api.created)

        monkeypatch.setattr(ship, "open_db", lambda: wiped)
        assert derive(wiped) == uids                    # same deterministic uids
        assert all(s is None for s in local_slugs(wiped).values())
        ship_all(ship, monkeypatch, uids)

        assert api.created == created                   # no duplicate pending rows
        assert local_slugs(wiped) == before
        assert local_refs(wiped) == {u: ingest_ref.ref_for(u) for u in before}
        assert api.ref_patches() == [] and failures(wiped) == []

    def test_a_lookup_miss_is_bound_by_the_creates_409(
            self, ship, monkeypatch, conn, wiped):  # noqa: F811
        """A soft-deleted event is excluded from the `?ingest_ref=` list, but
        TownLoops still holds its ref: the create's 409 names the row, and the
        event is bound to it with no further request."""
        api = _use(monkeypatch, FakeAPI())
        uids = derive(conn)
        ship_all(ship, monkeypatch, uids)
        jazz_slug = local_slugs(conn)[uids[0]]
        api.delete("events", jazz_slug)
        created = list(api.created)

        monkeypatch.setattr(ship, "open_db", lambda: wiped)
        derive(wiped)
        monkeypatch.setattr(ship, "parse_event_uid_arg", lambda: uids[0])
        api.calls.clear()
        assert ship.main() == 0

        assert api.created == created
        assert api.calls[-1][:2] == ("POST", "events")   # bound on the 409, no re-read
        row = wiped.execute("SELECT * FROM events WHERE event_uid = ?", (uids[0],)).fetchone()
        assert row["townloops_slug"] == jazz_slug and row["pushed_at"]
        assert row["ingest_ref"] == ingest_ref.ref_for(uids[0])
        [bound] = wiped.execute("SELECT detail FROM agent_activity "
                                "WHERE action = 'bound-by-ref'").fetchall()
        assert jazz_slug in bound[0]


class TestRenamedOnTownLoops:
    """A curator renames every live row (the old slugs now 404, naming no
    survivor). The next cycle finds each by its ref: no duplicates, nothing
    flagged missing, no 404-driven failure, local slugs re-pointed."""

    def _rename_all(self, api, conn):
        renamed = {}
        for uid, slug in local_slugs(conn).items():
            resource = next((r for r in RESOURCES if slug in api.tables.get(r, {})), None)
            api.rename(resource, slug, f"{slug}-renamed")
            renamed[uid] = f"{slug}-renamed"
        return renamed

    @pytest.mark.parametrize("sync_first", [True, False])
    def test_a_rename_rebinds_without_duplicates_or_404s(
            self, ship, monkeypatch, conn, sync_first):  # noqa: F811
        api = _use(monkeypatch, FakeAPI())
        uids = derive(conn)
        ship_all(ship, monkeypatch, uids)
        created = list(api.created)
        renamed = self._rename_all(api, conn)
        for _, table, _, _, _ in ENTITIES:   # each needs a sync (as after enrichment)
            conn.execute(f"UPDATE {table} SET pushed_hash = NULL")

        if sync_first:
            assert entity_sync.sweep(api, conn) == {"synced": len(ENTITIES)}
        ship_all(ship, monkeypatch, uids)
        assert entity_sync.sweep(api, conn) == {}       # nothing left out of sync

        assert api.created == created
        assert local_slugs(conn) == renamed
        assert flags(conn) == [] and failures(conn) == []
        for _, table, euid, _, _ in ENTITIES:
            r = conn.execute(f"SELECT * FROM {table} WHERE entity_uid = ?", (euid,)).fetchone()
            assert r["pushed_hash"] == r["content_hash"]
            assert json.loads(r["extra_json"])["live_id"] == \
                api.tables[table][renamed[euid]]["id"]


class TestShippedBeforeRefs:
    """Rows shipped before refs existed (live rows with no ref, local rows
    with a cached slug) are each given their ref once, via the set-once PATCH."""

    def test_each_pre_ref_row_is_adopted_once(self, ship, monkeypatch, conn):  # noqa: F811
        api = _use(monkeypatch, FakeAPI())
        uids = derive(conn)
        ship_all(ship, monkeypatch, uids)
        slugs = local_slugs(conn)
        for uid, slug in slugs.items():                 # as if shipped before refs
            api.tables[_resource(uid, uids)][slug].pop("ingest_ref")
        conn.execute("UPDATE events SET ingest_ref = NULL")
        for _, table, _, _, _ in ENTITIES:
            conn.execute(f"UPDATE {table} SET ingest_ref = NULL")
        created = list(api.created)

        ship_all(ship, monkeypatch, uids)
        assert sorted(api.ref_patches()) == sorted(
            (_resource(u, uids), s, ingest_ref.ref_for(u)) for u, s in slugs.items())
        assert local_refs(conn) == {u: ingest_ref.ref_for(u) for u in slugs}

        ship_all(ship, monkeypatch, uids)               # found by ref from now on
        assert len(api.ref_patches()) == len(slugs)
        assert api.created == created and local_slugs(conn) == slugs
        assert failures(conn) == []

    def test_a_ref_already_taken_is_reported_not_forced(self, ship, monkeypatch, conn):  # noqa: F811
        """The fallback finds a pre-ref row, but another (deleted) row holds
        our ref: the adoption is recorded as failed, the event still ships."""
        api = _use(monkeypatch, FakeAPI())
        uids = derive(conn)
        ship_all(ship, monkeypatch, uids)
        slug = local_slugs(conn)[uids[0]]
        api.events[slug].pop("ingest_ref")
        api.deleted[("events", "old-copy")] = {"slug": "old-copy",
                                               "ingest_ref": ingest_ref.ref_for(uids[0])}
        conn.execute("UPDATE events SET ingest_ref = NULL")
        monkeypatch.setattr(ship, "parse_event_uid_arg", lambda: uids[0])
        assert ship.main() == 0
        assert api.ref_of("events", slug) is None
        [(worker, action, error)] = failures(conn)
        assert action == "adopt-ref" and "old-copy" in error

        # Final: marked on the row, and the next ship doesn't try again.
        row = conn.execute("SELECT * FROM events WHERE event_uid = ?", (uids[0],)).fetchone()
        assert ingest_ref.final(row) == "taken"
        attempts = len(api.ref_patches())
        assert ship.main() == 0
        assert len(api.ref_patches()) == attempts
        assert len(failures(conn)) == 1

    def test_an_entity_whose_ref_is_taken_is_not_retried_every_ship(
            self, ship, monkeypatch, conn):  # noqa: F811
        api = _use(monkeypatch, FakeAPI())
        uids = derive(conn)
        ship_all(ship, monkeypatch, uids)
        venue_uid = ENTITIES[0][2]
        vslug = local_slugs(conn)[venue_uid]
        api.venues[vslug].pop("ingest_ref")
        api.deleted[("venues", "old-venue")] = {"slug": "old-venue",
                                                "ingest_ref": ingest_ref.ref_for(venue_uid)}
        conn.execute("UPDATE venues SET ingest_ref = NULL")

        ship_all(ship, monkeypatch, uids)                  # two events, one venue

        assert [p for p in api.ref_patches() if p[0] == "venues"] == [
            ("venues", vslug, ingest_ref.ref_for(venue_uid))]
        row = conn.execute("SELECT * FROM venues WHERE entity_uid = ?", (venue_uid,)).fetchone()
        assert ingest_ref.final(row) == "taken" and row["ingest_ref"] is None
        [(worker, action, error)] = failures(conn)
        assert action == "adopt-ref" and "old-venue" in error
