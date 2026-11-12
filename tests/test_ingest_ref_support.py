"""Ingest Ref lookup hardening (#64). The live API takes refs on events only:
its venue/business/community/performer/creator lists ignore `?ingest_ref=`
and answer unrelated rows, and SLT bound new venues to the first of them
(`aleworx-stateline`), then synced their website and category onto it.

- A ref lookup accepts only a row that carries the ref; rows that don't are
  "filter ignored" — no match — and, from a resource that declares the
  filter, an autonomous town's circuit opens.
- Which resources take refs comes from the OpenAPI snapshot
  (`ingest_ref.supported`); elsewhere there is no lookup, no ref on a create,
  no adoption. No snapshot: events only.
- `entity-live-sync audit-refs` lists the bindings the bug may have made."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from engine import autonomy, entity_ship, ingest_ref, ref_adoption
from engine.derive_helpers import insert_entity, record_activity
from tests.fake_townloops import REF_RESOURCES, FakeAPI, write_openapi
from tests.test_ingest_fork_ingest_ref import _entity, _event, _rows, ingest, server  # noqa: F401
from tests.test_live_lookup import _stage, _use, conn, ship, snapshot  # noqa: F401 — fixtures

ROOT = Path(__file__).resolve().parent.parent
ALEWORX = {"id": 74, "slug": "aleworx-stateline", "name": "AleWorX Stateline",
           "website": "https://aleworx.example", "category_slug": "food-drink"}
SCHOOL = {"name": "South Tahoe High School", "website": "https://sths.example",
          "category_slug": "family"}


def _ref_lists(api, resource):
    return [c for c in api.calls if c[0] == "LIST" and c[1] == resource and "ingest_ref" in c[2]]


def _venue_row(conn):
    return conn.execute("SELECT * FROM venues WHERE entity_uid = 'v-uid'").fetchone()


# --------------------------------------------------------------------------
# The real API (no snapshot, FakeAPI's default): events only
# --------------------------------------------------------------------------

class TestTheRealAPI:
    def test_a_new_venue_is_never_bound_to_an_unrelated_live_one(self, ship, monkeypatch, conn):
        """SLT, replayed: the first live venue is AleWorX, carrying no ref."""
        api = _use(monkeypatch, FakeAPI(venues=[dict(ALEWORX)]))
        _stage(ship, monkeypatch, conn, venue=SCHOOL)
        assert ship.main() == 0

        assert api.venues["aleworx-stateline"] == ALEWORX            # untouched
        assert not [c for c in api.calls if c[0] == "PATCH" and c[2] == "aleworx-stateline"]
        assert _ref_lists(api, "venues") == []                        # no ref lookup at all
        venue_post, event_post = api.creates()
        assert "ingest_ref" not in venue_post                         # none sent where none taken
        row = _venue_row(conn)
        assert row["townloops_slug"] == "south-tahoe-high-school" and row["ingest_ref"] is None
        # The event keeps its own ref; it names its venue by the created id.
        assert event_post["ingest_ref"] and "venue_ingest_ref" not in event_post
        assert event_post["venue_id"] == api.venues["south-tahoe-high-school"]["id"]
        assert conn.execute("SELECT COUNT(*) FROM agent_activity "
                            "WHERE action = 'adopt-ref'").fetchone()[0] == 0

    def test_reshipping_finds_the_venue_by_its_cached_slug(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI(venues=[dict(ALEWORX)]))
        _stage(ship, monkeypatch, conn, venue=SCHOOL)
        assert ship.main() == 0
        slug = _venue_row(conn)["townloops_slug"]
        assert entity_ship.ship_entity(api, conn, "venue", "v-uid") == slug
        assert len([c for c in api.creates() if "name" in c]) == 1

    def test_a_pre_ref_venue_is_not_adopted(self, conn):
        api = FakeAPI(venues=[dict(ALEWORX)])
        insert_entity(conn, "venues", "v-uid", {"name": "AleWorX Stateline", "source": "derived",
                                                "source_kind": "derived",
                                                "townloops_slug": "aleworx-stateline",
                                                "pushed_at": "2026-10-01T00:00:00+00:00"})
        result = ingest_ref.adopt_and_remember(api, conn, "venue", "v-uid", dict(ALEWORX),
                                               worker="test")
        assert result.status == "unsupported" and api.ref_patches() == []
        row = _venue_row(conn)
        assert row["ingest_ref"] is None and ingest_ref.final(row) is None
        assert "ref_adoption" not in json.loads(row["extra_json"] or "{}")
        assert conn.execute("SELECT COUNT(*) FROM agent_activity").fetchone()[0] == 0
        assert ref_adoption.adopt_shipped(api, conn) == {}            # the sweep skips venues
        assert api.calls == []

    def test_the_event_ingest_fork_sends_refs_only_on_events(self, ingest, conn, server):  # noqa: F811
        venue_uid = ingest._entity_uid("venue", "Riva Grill")
        _entity(conn, "venue", name="Riva Grill", uid=venue_uid)
        _event(conn, venue="Riva Grill")
        (vrow,) = ingest._select_unpushed_by_kind(conn, "venue")
        assert "ingest_ref" not in ingest._row_to_bulk_entry(vrow, "venue", conn)["payload"]
        (erow,) = ingest._select_unpushed_by_kind(conn, "event")
        payload = ingest._row_to_bulk_entry(erow, "event", conn)["payload"]
        assert payload["ingest_ref"] and "venue_ingest_ref" not in payload

        ingest._bulk_post(conn, "venue", [vrow], "https://tl.example/entries/bulk", "k", 100, False)
        (row,) = _rows(conn, "venue")
        assert row["townloops_slug"] and row["ingest_ref"] is None    # nothing it doesn't carry


# --------------------------------------------------------------------------
# A lookup accepts only a row carrying the ref
# --------------------------------------------------------------------------

@pytest.mark.usefixtures("refs_everywhere")
class TestCarriesTheRef:
    def test_rows_not_carrying_the_ref_are_filter_ignored_not_a_match(self):
        api = FakeAPI(venues=[dict(ALEWORX)], ignored_filters={"venues": {"ingest_ref"}})
        seen = []
        api.ref_filter_ignored = lambda resource, evidence: seen.append((resource, evidence))
        found = ingest_ref.lookup_by_ref(api, "venues", "hermes-slt-0123")
        assert (found.row, found.status) == (None, "ignored")
        assert ingest_ref.find_by_ref(api, "venues", "hermes-slt-0123") is None
        assert seen[0][0] == "venues"
        assert seen[0][1]["returned"] == [{"slug": "aleworx-stateline", "ingest_ref": None}]

    def test_the_row_carrying_it_is_found_among_others(self):
        api = FakeAPI(venues=[dict(ALEWORX), {"id": 8, "slug": "riva", "name": "Riva",
                                              "ingest_ref": "r"}],
                      ignored_filters={"venues": {"ingest_ref"}})
        assert ingest_ref.lookup_by_ref(api, "venues", "r").row["slug"] == "riva"

    def test_two_rows_carrying_one_ref_is_never_a_pick(self):
        api = FakeAPI(venues=[{"id": 1, "slug": "a", "name": "A", "ingest_ref": "r"},
                              {"id": 2, "slug": "b", "name": "B", "ingest_ref": "r"}])
        with pytest.raises(ingest_ref.RefNotUnique):
            ingest_ref.lookup_by_ref(api, "venues", "r")

    def test_an_ignored_filter_where_declared_opens_the_circuit(self, conn):
        """Autonomous: the server broke the contract the snapshot declares —
        no binding, and submissions stop (the venue's create is held)."""
        api = FakeAPI(venues=[dict(ALEWORX)], ignored_filters={"venues": {"ingest_ref"}})
        alerts = []
        gated = autonomy.GatedClient(api, conn, autonomy.Settings(mode="autonomous"),
                                     alert=alerts.append)
        insert_entity(conn, "venues", "v-uid", {"source": "derived", "source_kind": "derived",
                                                **SCHOOL})
        with pytest.raises(autonomy.SubmissionsHeld):
            entity_ship.ship_entity(gated, conn, "venue", "v-uid")
        assert autonomy.circuit(conn)["reason"] == "ingest-ref-filter-ignored"
        assert len(alerts) == 1 and "ingest-ref-filter-ignored" in alerts[0]
        assert _venue_row(conn)["townloops_slug"] is None
        assert api.venues["aleworx-stateline"] == ALEWORX and api.creates() == []

    def test_supervised_an_ignored_filter_falls_back_and_creates(self, conn, capsys):
        api = FakeAPI(venues=[dict(ALEWORX)], ignored_filters={"venues": {"ingest_ref"}})
        insert_entity(conn, "venues", "v-uid", {"source": "derived", "source_kind": "derived",
                                                **SCHOOL})
        assert entity_ship.ship_entity(api, conn, "venue", "v-uid") == "south-tahoe-high-school"
        assert api.venues["aleworx-stateline"] == ALEWORX
        assert "filter ignored" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Capabilities from the OpenAPI snapshot
# --------------------------------------------------------------------------

class TestCapabilities:
    def test_no_snapshot_is_events_only(self, tmp_state_dir):
        assert ingest_ref.supported("events") and ingest_ref.supported("event")
        assert not any(ingest_ref.supported(r) for r in REF_RESOURCES[1:])
        assert not ingest_ref.accepts_venue_ref()

    def test_the_snapshot_decides_and_a_new_one_is_reread(self, tmp_state_dir):
        write_openapi(FakeAPI(ref_resources=("events", "venues")), tmp_state_dir)
        assert ingest_ref.supported("venues") and ingest_ref.supported("venue")
        assert ingest_ref.accepts_venue_ref() and not ingest_ref.supported("businesses")
        write_openapi(FakeAPI(ref_resources=("events", "businesses", "communities")), tmp_state_dir)
        assert not ingest_ref.supported("venues") and ingest_ref.supported("business")
        assert not ingest_ref.accepts_venue_ref()

    def test_an_unreadable_snapshot_is_events_only(self, tmp_state_dir):
        (tmp_state_dir / "openapi.json").write_text("{not json")
        assert ingest_ref.supported("events") and not ingest_ref.supported("venues")

    def test_the_filter_and_the_property_are_both_needed(self):
        spec = {
            "paths": {
                "/api/v1/towns/{town_slug}/events": {
                    "parameters": [{"$ref": "#/components/parameters/Ref"}],
                    "get": {"parameters": [{"name": "limit", "in": "query"}]},
                    "post": {"requestBody": {"content": {"application/json": {"schema": {
                        "allOf": [{"$ref": "#/components/schemas/EventPayload"}]}}}}}},
                "/api/v1/towns/{town_slug}/venues": {     # the property, but no filter
                    "get": {"parameters": [{"name": "limit", "in": "query"}]},
                    "post": {"requestBody": {"content": {"application/json": {"schema": {
                        "properties": {"ingest_ref": {}}}}}}}},
                "/api/v1/towns/{town_slug}/businesses": {  # the filter, but no property
                    "get": {"parameters": [{"name": "ingest_ref", "in": "query"}]},
                    "post": {"requestBody": {"content": {"application/json": {"schema": {
                        "properties": {"name": {}}}}}}}}},
            "components": {
                "parameters": {"Ref": {"name": "ingest_ref", "in": "query"}},
                "schemas": {"EventPayload": {"properties": {"ingest_ref": {},
                                                            "venue_ingest_ref": {}}}}}}
        cap = ingest_ref.parse_capabilities(spec)
        assert cap.resources == {"events"}
        assert cap.venue_ref is False        # venues take no refs, so no venue ref either


# --------------------------------------------------------------------------
# The read-only audit of bindings the bug may have made
# --------------------------------------------------------------------------

def _bound(conn, uid, name, slug, ref=None):
    insert_entity(conn, "venues", uid, {"name": name, "source": "derived",
                                        "source_kind": "derived", "townloops_slug": slug,
                                        "ingest_ref": ref,
                                        "pushed_at": "2026-11-01T02:19:00+00:00"})


def test_audit_lists_the_contaminated_bindings_and_writes_nothing(conn, monkeypatch, capsys):
    api = FakeAPI(venues=[dict(ALEWORX), {"id": 9, "slug": "riva", "name": "Riva Grill"}])
    _bound(conn, "school-uid", "South Tahoe High School", "aleworx-stateline",
           ref=ingest_ref.ref_for("school-uid"))
    _bound(conn, "valhalla-uid", "Valhalla Boathouse Theatre", "aleworx-stateline",
           ref=ingest_ref.ref_for("valhalla-uid"))
    _bound(conn, "riva-uid", "Riva Grill", "riva")                    # clean
    record_activity(conn, "entity-sync", "synced", "venue:school-uid",
                    detail="aleworx-stateline: pulled name; pushed category_slug,website")
    record_activity(conn, "entity-sync", "synced", "venue:valhalla-uid",
                    detail="aleworx-stateline: pulled name; pushed -")
    before = conn.total_changes

    for k in ("TOWNLOOPS_API_URL", "TOWNLOOPS_API_KEY"):
        monkeypatch.setenv(k, "x")
    spec = importlib.util.spec_from_file_location(
        "els_sync_audit", ROOT / "skills/business/entity-live-sync/sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import engine.townloops_api as tl
    monkeypatch.setattr(tl, "Client", lambda: api)
    monkeypatch.setattr(mod, "open_db", lambda: conn)
    monkeypatch.setattr(sys, "argv", ["sync.py", "audit-refs"])
    assert mod.main() == 0
    out = json.loads(capsys.readouterr().out)

    assert [e["entity_uid"] for e in out] == ["school-uid", "valhalla-uid"]
    school = out[0]
    assert school["reasons"] == ["shared-slug", "ref-unsupported", "ref-not-carried"]
    assert school["slug"] == "aleworx-stateline" and school["live_name"] == "AleWorX Stateline"
    assert school["shares_with"] == ["valhalla-uid"]
    assert [p["detail"] for p in school["patched"]] == [
        "aleworx-stateline: pulled name; pushed category_slug,website"]
    assert out[1]["patched"] == []
    assert {c[0] for c in api.calls} == {"GET"} and conn.total_changes == before
