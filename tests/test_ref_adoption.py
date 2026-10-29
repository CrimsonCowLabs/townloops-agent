"""engine/ref_adoption — rows the agent shipped before Ingest Refs existed are
given theirs retroactively through the set-once PATCH (#63, townloops ADR
0011), so recovery has one regime: find by ref."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from engine import ingest_ref, ref_adoption
from engine.derive_helpers import insert_entity
from engine.townloops_api import TownloopsError


ROOT = Path(__file__).resolve().parent.parent


class RefAPI:
    """The slice of TownLoops the sweep touches: GET (merged rows 404 naming
    their survivor), and PATCH of `ingest_ref` with the set-once rules —
    the identical ref is a no-op, another one on a set row is a 422, a ref
    another row holds is a 409 `ingest_ref_taken` naming that row."""

    def __init__(self, **tables):
        self.tables = {res: {r["slug"]: dict(r) for r in rows} for res, rows in tables.items()}
        self.merged_into: dict[tuple[str, str], str] = {}
        self.calls: list[tuple] = []

    def verify_town(self, *a, **k):
        return {"town_slug": "slt"}

    def get(self, resource, slug):
        self.calls.append(("GET", resource, slug))
        table = self.tables.get(resource, {})
        if slug not in table:
            into = self.merged_into.get((resource, slug))
            raise TownloopsError(404, "not_found", f"{slug} not found",
                                 {"merged_into_slug": into} if into else None)
        return copy.deepcopy(table[slug])

    def update(self, resource, slug, payload):
        self.calls.append(("PATCH", resource, slug, copy.deepcopy(payload)))
        table = self.tables.get(resource, {})
        if slug not in table:
            raise TownloopsError(404, "not_found", f"{slug} not found")
        ref = payload.get("ingest_ref")
        holder = next((s for s, r in table.items() if r.get("ingest_ref") == ref and s != slug), None)
        if holder:
            raise TownloopsError(409, "ingest_ref_taken", "taken",
                                 {"ingest_ref": ref, "slug": holder})
        current = table[slug].get("ingest_ref")
        if current and current != ref:
            raise TownloopsError(422, "validation_error", "ingest_ref is set once")
        table[slug]["ingest_ref"] = ref
        return copy.deepcopy(table[slug])

    def patches(self):
        return [c for c in self.calls if c[0] == "PATCH"]


@pytest.fixture
def conn(fresh_db, monkeypatch):
    monkeypatch.setenv("TOWNLOOPS_TOWN_SLUG", "slt")
    return fresh_db


def shipped_event(conn, uid="e" * 24, slug="jazz-night", pushed_at="2026-09-01T00:00:00+00:00",
                  **kw):
    cols = {"event_uid": uid, "source": "seed:x", "source_kind": "scraped", "name": "Jazz Night",
            "date": "2026-11-01", "date_day": "2026-11-01", "first_seen_at": "t",
            "last_seen_at": "t", "content_hash": "h", "last_modified_at": "t",
            "townloops_slug": slug, "pushed_at": pushed_at, "state": "shipped", **kw}
    conn.execute(f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                 tuple(cols.values()))


def shipped_venue(conn, uid="v" * 24, slug="the-mill", pushed_at="2026-09-01T00:00:00+00:00",
                  table="venues", **kw):
    insert_entity(conn, table, uid, {"source": "derived", "source_kind": "derived",
                                     "name": "The Mill", "townloops_slug": slug,
                                     "pushed_at": pushed_at, **kw})


def local(conn, table, uid):
    col = "event_uid" if table == "events" else "entity_uid"
    return dict(conn.execute(f"SELECT * FROM {table} WHERE {col} = ?", (uid,)).fetchone())


def activities(conn, action=None, status=None):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM agent_activity WHERE worker = 'ref-adoption' ORDER BY id")]
    return [r for r in rows if (action is None or r["action"] == action)
            and (status is None or f": {status}" in r["detail"])]


def test_pre_ref_event_and_venue_are_adopted(conn):
    shipped_event(conn)
    shipped_venue(conn)
    api = RefAPI(events=[{"slug": "jazz-night"}], venues=[{"slug": "the-mill"}])

    counts = ref_adoption.adopt_shipped(api, conn)

    eref, vref = ingest_ref.ref_for("e" * 24), ingest_ref.ref_for("v" * 24)
    assert sorted(api.patches()) == sorted([
        ("PATCH", "events", "jazz-night", {"ingest_ref": eref}),
        ("PATCH", "venues", "the-mill", {"ingest_ref": vref})])
    assert local(conn, "events", "e" * 24)["ingest_ref"] == eref
    assert local(conn, "venues", "v" * 24)["ingest_ref"] == vref
    assert counts["adopted"] == 2
    assert activities(conn, "sweep")


def test_every_entity_kind_is_covered(conn):
    for i, table in enumerate(("businesses", "communities", "performers", "creators")):
        shipped_venue(conn, uid=f"{i}" * 24, slug=f"row-{i}", table=table)
    api = RefAPI(**{t: [{"slug": f"row-{i}"}] for i, t in
                    enumerate(("businesses", "communities", "performers", "creators"))})
    assert ref_adoption.adopt_shipped(api, conn)["adopted"] == 4


def test_a_row_already_carrying_our_ref_is_remembered_without_a_patch(conn):
    shipped_venue(conn)
    ref = ingest_ref.ref_for("v" * 24)
    api = RefAPI(venues=[{"slug": "the-mill", "ingest_ref": ref}])
    assert ref_adoption.adopt_shipped(api, conn)["already"] == 1
    assert api.patches() == []
    assert local(conn, "venues", "v" * 24)["ingest_ref"] == ref


def test_another_ref_is_not_repointed_and_recorded_once(conn):
    shipped_venue(conn)
    api = RefAPI(venues=[{"slug": "the-mill", "ingest_ref": "cmo-the-mill"}])
    assert ref_adoption.adopt_shipped(api, conn)["other-ref"] == 1
    assert api.patches() == []
    assert local(conn, "venues", "v" * 24)["ingest_ref"] is None
    assert api.tables["venues"]["the-mill"]["ingest_ref"] == "cmo-the-mill"

    api.calls.clear()
    assert ref_adoption.adopt_shipped(api, conn).get("other-ref", 0) == 0
    assert api.calls == []
    assert len(activities(conn, "adopt-ref", "other-ref")) == 1


def test_a_taken_ref_is_flagged_once_and_not_retried(conn):
    shipped_event(conn)
    ref = ingest_ref.ref_for("e" * 24)
    api = RefAPI(events=[{"slug": "jazz-night"}, {"slug": "jazz-night-2", "ingest_ref": ref}])

    assert ref_adoption.adopt_shipped(api, conn)["taken"] == 1
    assert local(conn, "events", "e" * 24)["ingest_ref"] is None
    flag = json.loads(local(conn, "events", "e" * 24)["extra_json"])["ref_adoption"]
    assert flag["status"] == "taken" and "jazz-night-2" in flag["detail"]
    [act] = activities(conn, "adopt-ref", "taken")
    assert act["ok"] == 0 and "jazz-night-2" in act["error"]

    api.calls.clear()
    ref_adoption.adopt_shipped(api, conn)
    assert api.calls == []
    assert len(activities(conn, "adopt-ref")) == 1


def test_a_404_with_no_survivor_is_unbound_and_not_flagged(conn):
    shipped_event(conn)
    api = RefAPI(events=[])
    counts = ref_adoption.adopt_shipped(api, conn)
    assert counts["unbound"] == 1
    r = local(conn, "events", "e" * 24)
    assert r["ingest_ref"] is None and r["townloops_slug"] == "jazz-night"
    assert "live_sync" not in json.loads(r["extra_json"] or "{}")
    assert not [a for a in activities(conn) if a["ok"] == 0]


def test_a_merged_away_entity_is_repointed_then_adopted_on_the_survivor(conn):
    shipped_venue(conn, slug="hermes-slt-old")
    api = RefAPI(venues=[{"id": 9, "slug": "the-mill"}])
    api.merged_into[("venues", "hermes-slt-old")] = "the-mill"

    assert ref_adoption.adopt_shipped(api, conn)["adopted"] == 1

    r = local(conn, "venues", "v" * 24)
    assert r["townloops_slug"] == "the-mill"
    assert json.loads(r["extra_json"])["live_id"] == 9
    assert r["ingest_ref"] == ingest_ref.ref_for("v" * 24)
    assert api.patches() == [("PATCH", "venues", "the-mill",
                              {"ingest_ref": ingest_ref.ref_for("v" * 24)})]


def test_rows_that_are_not_ours_are_untouched(conn):
    # A live mirror (someone else's row), a cross-town row, a row the operator
    # has flagged, a duplicate copy riding a sibling's live row, a superseded
    # row, and a row never shipped.
    shipped_venue(conn, uid="m" * 24, slug="mirror", source_kind="live")
    shipped_venue(conn, uid="x" * 24, slug="reno-hall", extra_json=json.dumps({"cross_town": "reno"}))
    shipped_venue(conn, uid="f" * 24, slug="gone",
                  extra_json=json.dumps({"live_sync": {"status": "missing"}}))
    shipped_venue(conn, uid="n" * 24, slug=None, pushed_at=None)
    shipped_event(conn, uid="d" * 24, status="duplicate")
    shipped_event(conn, uid="s" * 24, slug="old-date", status="superseded")
    api = RefAPI(venues=[{"slug": s} for s in ("mirror", "reno-hall", "gone")],
                 events=[{"slug": "jazz-night"}, {"slug": "old-date"}])

    assert ref_adoption.adopt_shipped(api, conn) == {}
    assert api.calls == []


def test_the_limit_is_respected_oldest_first(conn):
    shipped_event(conn, uid="a" * 24, slug="e-new", pushed_at="2026-10-03T00:00:00+00:00")
    shipped_venue(conn, uid="b" * 24, slug="v-old", pushed_at="2026-10-01T00:00:00+00:00")
    shipped_venue(conn, uid="c" * 24, slug="v-mid", pushed_at="2026-10-02T00:00:00+00:00")
    api = RefAPI(events=[{"slug": "e-new"}], venues=[{"slug": "v-old"}, {"slug": "v-mid"}])

    assert ref_adoption.adopt_shipped(api, conn, limit=2)["adopted"] == 2
    assert [c[2] for c in api.patches()] == ["v-old", "v-mid"]
    api.calls.clear()
    assert ref_adoption.adopt_shipped(api, conn, limit=2)["adopted"] == 1
    assert [c[2] for c in api.patches()] == ["e-new"]


def test_a_failed_row_goes_behind_the_never_tried(conn):
    shipped_venue(conn, uid="b" * 24, slug="v-old", pushed_at="2026-10-01T00:00:00+00:00")
    shipped_venue(conn, uid="c" * 24, slug="v-new", pushed_at="2026-10-02T00:00:00+00:00")
    api = RefAPI(venues=[{"slug": "v-new"}])     # v-old: unbound (404)
    assert ref_adoption.adopt_shipped(api, conn, limit=1) == {"unbound": 1}
    assert ref_adoption.adopt_shipped(api, conn, limit=1) == {"adopted": 1}
    assert [c[2] for c in api.patches()] == ["v-new"]


def test_a_transient_error_is_retried(conn):
    shipped_venue(conn)
    api = RefAPI(venues=[{"slug": "the-mill"}])
    real = api.update
    api.update = lambda *a: (_ for _ in ()).throw(TownloopsError(503, "unavailable", "busy"))
    assert ref_adoption.adopt_shipped(api, conn) == {"error": 1}
    api.update = real
    assert ref_adoption.adopt_shipped(api, conn) == {"adopted": 1}


def test_a_second_run_does_nothing(conn):
    shipped_event(conn)
    shipped_venue(conn)
    api = RefAPI(events=[{"slug": "jazz-night"}], venues=[{"slug": "the-mill"}])
    ref_adoption.adopt_shipped(api, conn)
    api.calls.clear()
    n = len(activities(conn))
    assert ref_adoption.adopt_shipped(api, conn) == {}
    assert api.calls == []
    assert len(activities(conn)) == n


def test_entity_live_sync_run_adopts_refs(conn, monkeypatch, capsys):
    """The daily entity-live-sync run includes the adoption pass."""
    for k in ("TOWNLOOPS_API_URL", "TOWNLOOPS_API_KEY"):
        monkeypatch.setenv(k, "x")
    spec = importlib.util.spec_from_file_location(
        "els_sync", ROOT / "skills/business/entity-live-sync/sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    seen = {}
    monkeypatch.setitem(sys.modules, "engine.townloops_api",
                        types.SimpleNamespace(Client=lambda: "client"))
    monkeypatch.setattr(mod, "open_db", lambda: conn)
    monkeypatch.setattr(mod.entity_sync, "sweep", lambda *a, **k: {})
    monkeypatch.setattr(mod.entity_ship, "ship_released", lambda *a, **k: {})
    monkeypatch.setattr(mod.series_sync, "sweep", lambda *a, **k: {})
    monkeypatch.setattr(mod.autonomy, "gated", lambda c, conn: c)

    def fake_adopt(client, c, limit=25, **k):
        seen["call"] = (client, limit)
        return {"adopted": 3}
    monkeypatch.setattr(mod.ref_adoption, "adopt_shipped", fake_adopt)
    monkeypatch.setattr(sys, "argv", ["sync.py", "run", "--limit", "7"])

    assert mod.main() == 0
    assert seen["call"] == ("client", 7)
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["refs"] == {"adopted": 3}


class TestAgainstTheSharedFake:
    """The same sweep against tests/fake_townloops.py — the fake the ship
    tests run on — so the test-local RefAPI can't drift from it unseen."""

    def test_adopted_already_taken_and_merged(self, conn):
        from tests.fake_townloops import FakeAPI
        shipped_event(conn, uid="a" * 24, slug="jazz-night")
        shipped_event(conn, uid="b" * 24, slug="open-mic")
        shipped_event(conn, uid="c" * 24, slug="trivia")
        shipped_venue(conn, slug="hermes-slt-old")
        start = "2026-11-01T03:00:00+00:00"
        api = FakeAPI(
            events=[{"slug": "jazz-night", "title": "Jazz Night", "start_at": start},
                    {"slug": "open-mic", "title": "Open Mic", "start_at": start,
                     "ingest_ref": ingest_ref.ref_for("b" * 24)},
                    {"slug": "trivia", "title": "Trivia", "start_at": start},
                    {"slug": "trivia-2", "title": "Trivia", "start_at": start,
                     "ingest_ref": ingest_ref.ref_for("c" * 24)}],
            entities={"venues": [{"id": 9, "slug": "the-mill", "name": "The Mill"},
                                 {"id": 8, "slug": "hermes-slt-old", "name": "The Mill"}]})
        api.merge("venues", "hermes-slt-old", into="the-mill")

        counts = ref_adoption.adopt_shipped(api, conn)

        assert counts == {"adopted": 2, "already": 1, "taken": 1}
        assert api.ref_of("events", "jazz-night") == ingest_ref.ref_for("a" * 24)
        assert api.ref_of("events", "trivia") is None
        assert api.ref_of("venues", "the-mill") == ingest_ref.ref_for("v" * 24)
        assert local(conn, "venues", "v" * 24)["townloops_slug"] == "the-mill"
        assert local(conn, "events", "b" * 24)["ingest_ref"] == ingest_ref.ref_for("b" * 24)
        assert ingest_ref.final(local(conn, "events", "c" * 24)) == "taken"
        assert ref_adoption.adopt_shipped(api, conn) == {}

    def test_another_ref_is_left_alone(self, conn):
        from tests.fake_townloops import FakeAPI
        shipped_venue(conn)
        api = FakeAPI(entities={"venues": [{"id": 9, "slug": "the-mill", "name": "The Mill",
                                            "ingest_ref": "cmo-the-mill"}]})
        assert ref_adoption.adopt_shipped(api, conn) == {"other-ref": 1}
        assert api.ref_patches() == [] and api.ref_of("venues", "the-mill") == "cmo-the-mill"
