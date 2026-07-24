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
