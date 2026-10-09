"""townloops-api skill — structured diff logic + Client freeze guard.

Network I/O is not exercised — the CLI paths that hit the wire (snapshot,
check, query) are covered by the smoke-test log at commit time. This suite
tests the parts that would silently misbehave: diff classification and
the write-refusal semantics.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def api_mod():
    """Load skills/business/townloops-api/api.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "townloops_api_skill",
        REPO_ROOT / "skills" / "business" / "townloops-api" / "api.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_spec(paths: dict = None, schemas: dict = None) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Test API", "version": "1.0"},
        "paths": paths or {},
        "components": {"schemas": schemas or {}},
    }


class TestDiffPaths:
    def test_path_removed_is_breaking(self, api_mod):
        old = _make_spec(paths={"/api/v1/towns/{town_slug}/events": {"get": {}}})
        new = _make_spec(paths={})
        r = api_mod.diff_specs(old, new)
        assert r["counts"]["breaking"] == 1
        assert r["breaking"][0]["kind"] == "path_removed"
        assert "events" in r["resources_to_freeze"]

    def test_path_added_is_non_breaking(self, api_mod):
        old = _make_spec(paths={})
        new = _make_spec(paths={"/api/v1/towns/{town_slug}/venues": {"get": {}}})
        r = api_mod.diff_specs(old, new)
        assert r["counts"]["breaking"] == 0
        assert r["counts"]["paths_added"] == 1

    def test_operation_removed_is_breaking(self, api_mod):
        p = "/api/v1/towns/{town_slug}/businesses"
        old = _make_spec(paths={p: {"get": {}, "post": {}}})
        new = _make_spec(paths={p: {"get": {}}})
        r = api_mod.diff_specs(old, new)
        assert any(c["kind"] == "operation_removed" and c["method"] == "post"
                   for c in r["breaking"])
        assert "businesses" in r["resources_to_freeze"]

    def test_operation_added_is_non_breaking(self, api_mod):
        p = "/api/v1/towns/{town_slug}/businesses"
        old = _make_spec(paths={p: {"get": {}}})
        new = _make_spec(paths={p: {"get": {}, "delete": {}}})
        r = api_mod.diff_specs(old, new)
        assert r["counts"]["breaking"] == 0
        assert any(c["kind"] == "operation_added" and c["method"] == "delete"
                   for c in r["non_breaking"])


class TestDiffSchemas:
    def test_required_field_added_is_breaking(self, api_mod):
        old = _make_spec(schemas={"BusinessPayload": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}, "phone": {"type": "string"}},
        }})
        new = _make_spec(schemas={"BusinessPayload": {
            "type": "object",
            "required": ["name", "phone"],
            "properties": {"name": {"type": "string"}, "phone": {"type": "string"}},
        }})
        r = api_mod.diff_specs(old, new)
        assert any(c["kind"] == "required_added" for c in r["breaking"])
        assert "businesses" in r["resources_to_freeze"]

    def test_required_field_removed_is_non_breaking(self, api_mod):
        old = _make_spec(schemas={"EventPayload": {
            "type": "object",
            "required": ["name", "old_required"],
            "properties": {"name": {"type": "string"}, "old_required": {"type": "string"}},
        }})
        new = _make_spec(schemas={"EventPayload": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}, "old_required": {"type": "string"}},
        }})
        r = api_mod.diff_specs(old, new)
        # No breaking; loosening a requirement doesn't hurt us.
        assert r["counts"]["breaking"] == 0

    def test_property_removed_is_breaking(self, api_mod):
        old = _make_spec(schemas={"VenuePayload": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "gone": {"type": "string"}},
        }})
        new = _make_spec(schemas={"VenuePayload": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        }})
        r = api_mod.diff_specs(old, new)
        assert any(c["kind"] == "property_removed" for c in r["breaking"])
        assert "venues" in r["resources_to_freeze"]

    def test_type_change_is_breaking(self, api_mod):
        old = _make_spec(schemas={"SponsorPayload": {
            "type": "object",
            "properties": {"weight": {"type": "integer"}},
        }})
        new = _make_spec(schemas={"SponsorPayload": {
            "type": "object",
            "properties": {"weight": {"type": "string"}},
        }})
        r = api_mod.diff_specs(old, new)
        assert any(c["kind"] == "property_type_changed" for c in r["breaking"])
        assert "sponsors" in r["resources_to_freeze"]

    def test_schema_removed_is_breaking(self, api_mod):
        old = _make_spec(schemas={"AdSlotPayload": {"type": "object"}})
        new = _make_spec(schemas={})
        r = api_mod.diff_specs(old, new)
        assert any(c["kind"] == "schema_removed" for c in r["breaking"])
        assert "ad-slots" in r["resources_to_freeze"]


class TestResourceOfPath:
    def test_matches_resource(self, api_mod):
        assert api_mod._resource_of_path(
            "/api/v1/towns/{town_slug}/events"
        ) == "events"
        assert api_mod._resource_of_path(
            "/api/v1/towns/{town_slug}/ad-slots"
        ) == "ad-slots"

    def test_no_resource_for_healthz(self, api_mod):
        assert api_mod._resource_of_path("/api/v1/healthz") is None


class TestFreezeGuard:
    def test_no_freeze_file_allows_writes(self, tmp_state_dir, monkeypatch):
        from engine import townloops_api
        monkeypatch.setenv("TOWNLOOPS_API_FREEZE_PATH",
                           str(tmp_state_dir / "does_not_exist.json"))
        # bust the module-level cache
        townloops_api._FREEZE_CACHE.update(mtime=-1.0)
        townloops_api._guard_write("businesses")  # should not raise

    def test_frozen_resource_raises(self, tmp_state_dir, monkeypatch):
        from engine import townloops_api
        freeze_path = tmp_state_dir / "api_freeze.json"
        freeze_path.write_text(json.dumps({
            "frozen_at": "2026-01-01T00:00:00+00:00",
            "reason": "breaking",
            "resources": ["events", "businesses"],
        }))
        monkeypatch.setenv("TOWNLOOPS_API_FREEZE_PATH", str(freeze_path))
        townloops_api._FREEZE_CACHE.update(mtime=-1.0)
        with pytest.raises(townloops_api.TownloopsError) as exc:
            townloops_api._guard_write("events")
        assert exc.value.code == "resource_frozen"

    def test_non_frozen_resource_allowed(self, tmp_state_dir, monkeypatch):
        from engine import townloops_api
        freeze_path = tmp_state_dir / "api_freeze.json"
        freeze_path.write_text(json.dumps({
            "frozen_at": "2026-01-01T00:00:00+00:00",
            "reason": "breaking",
            "resources": ["events"],
        }))
        monkeypatch.setenv("TOWNLOOPS_API_FREEZE_PATH", str(freeze_path))
        townloops_api._FREEZE_CACHE.update(mtime=-1.0)
        townloops_api._guard_write("venues")  # not frozen — no raise

    def test_mtime_refresh_picks_up_new_freeze(self, tmp_state_dir, monkeypatch):
        import time
        from engine import townloops_api
        freeze_path = tmp_state_dir / "api_freeze.json"
        freeze_path.write_text(json.dumps({"resources": []}))
        monkeypatch.setenv("TOWNLOOPS_API_FREEZE_PATH", str(freeze_path))
        townloops_api._FREEZE_CACHE.update(mtime=-1.0)
        townloops_api._guard_write("events")  # allowed initially

        # bump mtime to force refresh
        time.sleep(0.02)
        freeze_path.write_text(json.dumps({"resources": ["events"]}))
        with pytest.raises(townloops_api.TownloopsError):
            townloops_api._guard_write("events")


# --------------------------------------------------------------------------
# Series contract (ADR 0007): additive arrival is fine; Series writes,
# writable membership or a vanished `series_slug` fail the check loudly.
# --------------------------------------------------------------------------

EVENTS_P = "/api/v1/towns/{town_slug}/events"
SERIES_P = "/api/v1/towns/{town_slug}/series"
SERIES_ONE_P = SERIES_P + "/{series_slug}"


def _event_schemas(resource_extra=(), patch_extra=()):
    props = {"slug": {"type": "string"}, "title": {"type": "string"}}
    return {
        "EventResource": {"properties": {**props, **{f: {"type": ["integer", "null"]
                                                     if f.endswith("_id") else ["string", "null"]}
                                                 for f in resource_extra}}},
        "EventPayload": {"properties": dict(props)},
        "EventPatch": {"properties": {**props, **{f: {"type": "string"} for f in patch_extra}}},
    }


PRE_SERIES = _make_spec(paths={EVENTS_P: {"get": {}, "post": {}}}, schemas=_event_schemas())
WITH_SERIES = _make_spec(
    paths={EVENTS_P: {"get": {}, "post": {}}, SERIES_P: {"get": {}}, SERIES_ONE_P: {"get": {}}},
    schemas={**_event_schemas(("series_slug", "series_id")),
             "SeriesResource": {"properties": {"slug": {"type": "string"}}}})


def _with(spec, paths=None, schemas=None):
    out = json.loads(json.dumps(spec))
    out["paths"].update(paths or {})
    out["components"]["schemas"].update(schemas or {})
    return out


class TestSeriesContract:
    def test_series_arriving_is_additive(self, api_mod):
        r = api_mod.diff_specs(PRE_SERIES, WITH_SERIES)
        assert r["counts"]["breaking"] == 0 and r["resources_to_freeze"] == []
        assert api_mod.series_contract(PRE_SERIES, WITH_SERIES) == []

    def test_a_series_write_operation_is_flagged(self, api_mod):
        new = _with(WITH_SERIES, paths={SERIES_P: {"get": {}, "post": {}}})
        [problem] = api_mod.series_contract(WITH_SERIES, new)
        assert "POST" in problem and "series" in problem

    def test_writable_membership_is_flagged(self, api_mod):
        new = _with(WITH_SERIES, schemas={"EventPatch": _event_schemas(
            patch_extra=("series_slug",))["EventPatch"]})
        [problem] = api_mod.series_contract(WITH_SERIES, new)
        assert "EventPatch.series_slug" in problem

    def test_series_slug_disappearing_is_flagged(self, api_mod):
        [problem] = api_mod.series_contract(WITH_SERIES, PRE_SERIES)
        assert "series_slug" in problem
        assert api_mod.series_contract(PRE_SERIES, PRE_SERIES) == []


@pytest.fixture
def check(tmp_state_dir, monkeypatch):
    """Run `api.py check` against `new` with `old` as the last snapshot."""
    spec = importlib.util.spec_from_file_location(
        "townloops_api_skill_check", REPO_ROOT / "skills" / "business" / "townloops-api" / "api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sent: list[str] = []
    monkeypatch.setattr(mod, "_telegram_notify", sent.append)

    def go(old, new, api=None):
        from tests.fake_townloops import FakeAPI
        import engine.townloops_api as tl
        monkeypatch.setattr(tl, "Client", lambda: api or FakeAPI())
        (tmp_state_dir / "openapi.json").write_text(json.dumps(old))
        monkeypatch.setattr(mod, "_fetch_openapi", lambda url: new)
        return mod.main(["check"])

    go.telegrams = sent
    go.state = tmp_state_dir
    return go


class TestCheckSeries:
    def test_additive_series_passes_and_refreshes_the_snapshot(self, check):
        from tests.fake_townloops import FakeAPI
        api = FakeAPI(series=[{"id": 1, "slug": "trivia", "title": "Trivia"},
                              {"id": 2, "slug": "old", "title": "Old", "is_active": False}])
        assert check(PRE_SERIES, WITH_SERIES, api) == 0
        assert not (check.state / "api_freeze.json").exists()
        snap = json.loads((check.state / "series.json").read_text())
        assert [s["slug"] for s in snap["series"]] == ["trivia"]

    def test_vanished_series_slug_freezes_events_and_fails(self, check):
        assert check(WITH_SERIES, PRE_SERIES) == 1
        freeze = json.loads((check.state / "api_freeze.json").read_text())
        assert "events" in freeze["resources"]
        assert any("Series contract" in t and "series_slug" in t for t in check.telegrams)

    def test_series_write_surface_fails_loudly(self, check):
        new = _with(WITH_SERIES, paths={SERIES_ONE_P: {"get": {}, "patch": {}}})
        assert check(WITH_SERIES, new) == 1
        assert any("PATCH" in t for t in check.telegrams)

    def test_a_standing_series_write_surface_alerts_once(self, check):
        new = _with(WITH_SERIES, paths={SERIES_ONE_P: {"get": {}, "patch": {}}})
        assert check(new, new) == 0
        assert not any("Series contract" in t for t in check.telegrams)


class TestClientNeverWritesSeries:
    @pytest.mark.parametrize("call", [
        lambda c: c.create("series", {"title": "x"}),
        lambda c: c.update("series", "x", {"title": "y"}),
        lambda c: c.delete("series", "x"),
    ])
    def test_writes_are_refused_before_the_wire(self, call, monkeypatch):
        from engine.townloops_api import Client, TownloopsError
        c = Client(base_url="http://test.invalid", api_key="k", town_slug="t")
        monkeypatch.setattr(c, "_request", lambda *a, **k: pytest.fail("reached the wire"))
        with pytest.raises(TownloopsError) as e:
            call(c)
        assert e.value.code == "read_only_resource"
